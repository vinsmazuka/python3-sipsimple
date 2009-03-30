#!/usr/bin/env python

import datetime
import os
import random
import select
import sys
import termios

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python.queue import EventQueue
from collections import deque
from optparse import OptionParser
from threading import Thread
from time import time
from twisted.python import threadable
from zope.interface import implements

from twisted.internet import reactor
from eventlet.twistedutil import join_reactor

from sipsimple import Engine, SIPCoreError, SIPURI, Subscription
from sipsimple.account import AccountManager
from sipsimple.clients.log import Logger
from sipsimple.lookup import DNSLookup
from sipsimple.configuration import ConfigurationManager
from sipsimple.configuration.settings import SIPSimpleSettings


class InputThread(Thread):
    def __init__(self, application):
        Thread.__init__(self)
        self.application = application
        self.daemon = True
        self._old_terminal_settings = None

    def run(self):
        notification_center = NotificationCenter()
        while True:
            for char in self._getchars():
                if char == "\x04":
                    self.application.stop()
                    return
                else:
                    notification_center.post_notification('SAInputWasReceived', sender=self, data=NotificationData(input=char))

    def stop(self):
        self._termios_restore()

    def _termios_restore(self):
        if self._old_terminal_settings is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_terminal_settings)

    def _getchars(self):
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            self._old_terminal_settings = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[3] = new[3] & ~termios.ICANON & ~termios.ECHO
            new[6][termios.VMIN] = '\000'
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, new)
                if select.select([fd], [], [], None)[0]:
                    return sys.stdin.read(4192)
            finally:
                self._termios_restore()
        else:
            return os.read(fd, 4192)


class SubscriptionApplication(object):
    implements(IObserver)

    def __init__(self, account_name, target, trace_sip, trace_pjsip):
        self.account_name = account_name
        self.target = target
        self.input = InputThread(self)
        self.output = EventQueue(lambda event: sys.stdout.write(event+'\n'))
        self.logger = Logger(trace_sip, trace_pjsip)
        self.success = False
        self.account = None
        self.subscription = None
        self.stopping = False

        self._subscription_routes = None
        self._subscription_timeout = 0.0
        self._subscription_wait = 0.5

        account_manager = AccountManager()
        engine = Engine()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=account_manager)
        notification_center.add_observer(self, sender=engine)
        notification_center.add_observer(self, sender=self.input)

        log.level.current = log.level.WARNING

    def run(self):
        account_manager = AccountManager()
        configuration = ConfigurationManager()
        engine = Engine()
        notification_center = NotificationCenter()
        
        # start output thread
        self.output.start()
    
        # startup configuration
        configuration.start()
        account_manager.start()
        if self.account is None:
            raise RuntimeError("unknown account %s. Available accounts: %s" % (self.account_name, ', '.join(account.id for account in account_manager.iter_accounts())))
        elif not self.account.enabled:
            raise RuntimeError("account %s is not enabled" % self.account.id)
        self.output.put('Using account %s' % self.account.id)
        settings = SIPSimpleSettings()

        # start logging
        self.logger.start()

        # start the engine
        engine.start(
            auto_sound=False,
            events={'presence': ['multipart/related', 'application/rlmi+xml', 'application/pidf+xml']},
            local_ip=settings.local_ip.normalized,
            local_udp_port=settings.sip.local_udp_port if "udp" in settings.sip.transports else None,
            local_tcp_port=settings.sip.local_tcp_port if "tcp" in settings.sip.transports else None,
            local_tls_port=settings.sip.local_tls_port if "tls" in settings.sip.transports else None,
            tls_protocol=settings.tls.protocol,
            tls_verify_server=settings.tls.verify_server,
            tls_ca_file=settings.tls.ca_list_file.normalized if settings.tls.ca_list_file is not None else None,
            tls_cert_file=settings.tls.certificate_file.normalized if settings.tls.certificate_file is not None else None,
            tls_privkey_file=settings.tls.private_key_file.normalized if settings.tls.private_key_file is not None else None,
            tls_timeout=settings.tls.timeout,
            ec_tail_length=settings.audio.echo_delay,
            user_agent=settings.user_agent,
            sample_rate=settings.audio.sample_rate,
            playback_dtmf=settings.audio.playback_dtmf,
            rtp_port_range=(settings.rtp.port_range.start, settings.rtp.port_range.end),
            trace_sip=settings.logging.trace_sip or self.logger.sip_to_stdout,
            log_level=settings.logging.pjsip_level if (settings.logging.trace_pjsip or self.logger.pjsip_to_stdout) else 0
        )

        if self.target is None:
            self.target = SIPURI(user='%s-buddies' % self.account.id.username, host=self.account.id.domain)
        else:
            if '@' not in self.target:
                self.target = '%s@%s' % (self.target, self.account.id.domain)
            if not self.target.startswith('sip:') and not self.target.startswith('sips:'):
                self.target = 'sip:' + self.target
            try:
                self.target = engine.parse_sip_uri(self.target)
            except SIPCoreError:
                self.output.put('Illegal SIP URI: %s' % self.target)
                engine.stop()
                return 1
        self.output.put('Subscribing to %s for the presence event' % self.target)

        # start the input thread
        self.input.start()

        reactor.callLater(0, self._subscribe)

        # start twisted
        try:
            reactor.run()
        finally:
            self.input.stop()
        
        # stop the output
        self.output.stop()
        self.output.join()
        
        self.logger.stop()

        return 0 if self.success else 1

    def stop(self):
        self.stopping = True
        account_manager = AccountManager()
        account_manager.stop()
        if self.subscription is not None and self.subscription.state.lower() in ('accepted', 'pending', 'active'):
            self.subscription.unsubscribe()
        else:
            if threadable.isInIOThread():
                reactor.stop()
            else:
                reactor.callFromThread(reactor.stop)

    def print_help(self):
        message  = 'Available control keys:\n'
        message += '  t: toggle SIP trace on the console\n'
        message += '  j: toggle PJSIP trace on the console\n'
        message += '  Ctrl-d: quit the program\n'
        message += '  ?: display this help message\n'
        self.output.put('\n'+message)
        
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, None)
        if handler is not None:
            handler(notification)

    def _NH_AMAccountWasAdded(self, notification):
        account = notification.data.account
        account_manager = AccountManager()
        if account.id == self.account_name or (self.account_name is None and account is account_manager.default_account):
            self.account = account
            account.registration.enabled = False
        else:
            account.enabled = False

    def _NH_SCSubscriptionChangedState(self, notification):
        route = notification.sender.route
        if notification.data.state.lower() in ('active', 'accepted'):
            self._subscription_routes = None
            self._subscription_wait = 0.5
            if not self.success:
                self.output.put('Subscription succeeded at %s:%d;transport=%s' % (route.address, route.port, route.transport))
                self.success = True
        elif notification.data.state.lower() == 'pending':
            self._subscription_routes = None
            self._subscription_wait = 0.5
            self.output.put('Subscription is pending at %s:%d;transport=%s' % (route.address, route.port, route.transport))
        elif notification.data.state.lower() == 'terminated':
            self.subscription = None
            if hasattr(notification.data, 'code'):
                status = ': %d %s' % (notification.data.code, notification.data.reason)
            else:
                status = ''
            self.output.put('Unsubscribed from %s:%d;transport=%s%s' % (route.address, route.port, route.transport, status))
            if self.stopping or notification.data.code in (401, 403, 407):
                if hasattr(notification.data, 'code') and notification.data.code / 100 == 2:
                    self.success = True
                self.stop()
            else:
                self.success = False
                if not self._subscription_routes or time() > self._subscription_timeout:
                    self._subscription_wait = min(self._subscription_wait*2, 30)
                    timeout = random.uniform(self._subscription_wait, 2*self._subscription_wait)
                    reactor.callFromThread(reactor.callLater, timeout, self._publish)
                else:
                    self.subscription = Subscription(self.account.credentials, self.target, "presence", route=self._subscription_routes.popleft(), expires=self.account.presence.subscribe_interval, extra_headers={'Supported': 'eventlist'})
                    notification_center = NotificationCenter()
                    notification_center.add_observer(self, sender=self.subscription)
                    self.subscription.subscribe()

    def _NH_SCSubscriptionGotNotify(self, notification):
        if ('%s/%s' % (notification.data.content_type, notification.data.content_subtype)) in ('multipart/related', 'application/rlmi+xml', 'application/pidf+xml'):
            self.output.put('Received NOTIFY:\n'+notification.data.body)
            self.print_help()

    def _NH_DNSLookupDidSucceed(self, notification):
        # create subscription and register to get notifications from it
        self._subscription_routes = deque(notification.data.result)
        self.subscription = Subscription(self.account.credentials, self.target, "presence", route=self._subscription_routes.popleft(), expires=self.account.presence.subscribe_interval, extra_headers={'Supported': 'eventlist'})
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.subscription)
        self.subscription.subscribe()

    def _NH_DNSLookupDidFail(self, notification):
        self.output.put('DNS lookup failed: %s' % notification.data.error)
        timeout = random.uniform(1.0, 2.0)
        reactor.callLater(timeout, self._subscribe)

    def _NH_SAInputWasReceived(self, notification):
        engine = Engine()
        settings = SIPSimpleSettings()
        key = notification.data.input
        if key == 't':
            self.logger.sip_to_stdout = not self.logger.sip_to_stdout
            engine.trace_sip = self.logger.sip_to_stdout or settings.logging.trace_sip
            self.output.put('SIP tracing to console is now %s.' % ('activated' if self.logger.sip_to_stdout else 'deactivated'))
        elif key == 'j':
            self.logger.pjsip_to_stdout = not self.logger.pjsip_to_stdout
            engine.log_level = settings.logging.pjsip_level if (self.logger.pjsip_to_stdout or settings.logging.trace_pjsip) else 0
            self.output.put('PJSIP tracing to console is now %s.' % ('activated' if self.logger.pjsip_to_stdout else 'deactivated'))
        elif key == '?':
            self.print_help()

    def _NH_SCEngineDidFail(self, notification):
        self.output.put('Engine failed.')
        if threadable.isInIOThread():
            reactor.stop()
        else:
            reactor.callFromThread(reactor.stop)

    def _NH_SCEngineGotException(self, notification):
        self.output.put('An exception occured within the SIP core:\n'+notification.data.traceback)
    
    def _subscribe(self):
        settings = SIPSimpleSettings()
        
        self._subscription_timeout = time()+30

        lookup = DNSLookup()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=lookup)
        if self.account.outbound_proxy is not None:
            uri = SIPURI(host=self.account.outbound_proxy.host, port=self.account.outbound_proxy.port, parameters={'transport': self.account.outbound_proxy.transport})
        else:
            uri = self.target
        lookup.lookup_sip_proxy(uri, settings.sip.transports)


if __name__ == "__main__":
    description = "This script will SUBSCRIBE to the presence event published by the specified SIP target assuming it is a resource list handled by a RLS server. The RLS server will then SUBSCRIBE in behalf of the account, collect NOTIFYs with the presence information of the recipients and provide periodically aggregated NOTIFYs back to the subscriber. If a target address is not specified, it will subscribe to the address 'username-buddies@domain.com', where username and domain are taken from the account's SIP address. It will then interprete PIDF bodies contained in NOTIFYs and display their meaning. The program will un-SUBSCRIBE and quit when CTRL+D is pressed."
    usage = "%prog [options] [target-user@target-domain.com]"
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    parser.add_option("-a", "--account-name", type="string", dest="account_name", help="The name of the account to use.")
    parser.add_option("-s", "--trace-sip", action="store_true", dest="trace_sip", default=False, help="Dump the raw contents of incoming and outgoing SIP messages (disabled by default).")
    parser.add_option("-j", "--trace-pjsip", action="store_true", dest="trace_pjsip", default=False, help="Print PJSIP logging output (disabled by default).")
    options, args = parser.parse_args()

    try:
        application = SubscriptionApplication(options.account_name, args[0] if args else None, options.trace_sip, options.trace_pjsip)
        return_code = application.run()
    except RuntimeError, e:
        print "Error: %s" % str(e)
        sys.exit(1)
    except SIPCoreError, e:
        print "Error: %s" % str(e)
        sys.exit(1)
    else:
        sys.exit(return_code)


