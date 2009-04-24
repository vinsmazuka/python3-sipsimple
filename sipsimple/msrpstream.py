import os
import random
from datetime import datetime
from collections import deque
from zope.interface import implements
from application.notification import NotificationCenter, NotificationData
from twisted.python.failure import Failure
from twisted.internet.error import ConnectionDone

from eventlet import proc
from msrplib.connect import get_acceptor, get_connector, MSRPRelaySettings
from msrplib.protocol import URI, FailureReportHeader, SuccessReportHeader, parse_uri
from msrplib.session import MSRPSession, contains_mime_type, OutgoingFile

from sipsimple.core import SDPAttribute, SDPMedia
from sipsimple.interfaces import IMediaStream
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.msrp import LoggerSingleton, get_X509Credentials
from sipsimple.clients.cpim import MessageCPIM, MessageCPIMParser
from sipsimple.clients.sdputil import FileSelector


class MSRPChatError(Exception):
    pass


class MSRPChat(object):
    implements(IMediaStream)

    def __init__(self, account, direction='sendrecv', file_selector=None):
        self.account = account
        self.direction = direction
        self.file_selector = file_selector
        self.notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        self.accept_types = list(settings.chat.accept_types)
        self.local_media = None
        self.msrp = None ## Placeholder for the MSRPTransport that will be set when started
        self.msrp_connector = None
        self.accept_wrapped_types = list(settings.chat.accept_wrapped_types)
        self.cpim_enabled = None             # Boolean value. None means it was not negotiated yet
        self.private_messages_allowed = None # Boolean value. None means it was not negotiated yet
        self.message_queue = deque() # messages stored here until the connection established
        self.session = None

    def make_SDPMedia(self, uri_path):
        attributes = []
        attributes.append(SDPAttribute("path", " ".join([str(uri) for uri in uri_path])))
        if self.direction not in [None, 'sendrecv']:
            attributes.append(SDPAttribute(self.direction, ''))
        if self.accept_types is not None:
            attributes.append(SDPAttribute("accept-types", " ".join(self.accept_types)))
        if self.accept_wrapped_types is not None:
            attributes.append(SDPAttribute("accept-wrapped-types", " ".join(self.accept_wrapped_types)))
        if self.file_selector is not None:
            attributes.append(SDPAttribute('file-selector', self.file_selector.format_sdp()))
        if uri_path[-1].use_tls:
            transport = "TCP/TLS/MSRP"
        else:
            transport = "TCP/MSRP"
        return SDPMedia("message", uri_path[-1].port or 12345, transport, formats=["*"], attributes=attributes)

    @property
    def from_uri(self):
        return self.account.credentials.uri

    def get_local_media(self, for_offer=True, on_hold=False):
        if on_hold:
            raise NotImplementedError
        return self.local_media

    def validate_incoming(self, remote_sdp, stream_index):
        media = remote_sdp.media[stream_index]
        media_attributes = dict((attr.name, attr.value) for attr in media.attributes)
        direction = media_attributes.get('direction', 'sendrecv')
        if direction != self.direction:
            return False
        return True

    def initialize(self, session):
        try:
            settings = SIPSimpleSettings()
            outgoing = session.direction == 'outgoing'
            if (outgoing and self.account.msrp.use_relay_for_outbound) or (not outgoing and self.account.msrp.use_relay_for_inbound):
                if self.account.msrp.relay is None:
                    relay = MSRPRelaySettings(domain=self.account.credentials.uri.host,
                                              username=self.account.credentials.uri.user,
                                              password=self.account.credentials.password)
                    self.transport = 'tls'
                else:
                    relay = MSRPRelaySettings(domain=self.account.credentials.uri.host,
                                              username=self.account.credentials.uri.user,
                                              password=self.account.credentials.password,
                                              host=self.account.msrp.relay.host,
                                              port=self.account.msrp.relay.port,
                                              use_tls=self.account.msrp.relay.transport=='tls')
                    self.transport = self.account.msrp.relay.transport
            else:
                relay = None
                self.transport = settings.msrp.transport
            logger = LoggerSingleton().logger
            self.msrp_connector = get_connector(relay=relay, logger=logger) if outgoing else get_acceptor(relay=relay, logger=logger)
            settings = SIPSimpleSettings()
            local_uri = URI(host=settings.local_ip.normalized,
                            port=settings.msrp.local_port,
                            use_tls=self.transport=='tls',
                            credentials=get_X509Credentials())
            full_local_path = self.msrp_connector.prepare(local_uri)
            self.local_media = self.make_SDPMedia(full_local_path)
            self.remote_uri = session.remote_uri
        except Exception, ex:
            ndata = NotificationData(context='initialize', failure=Failure(), reason=str(ex))
            self.notification_center.post_notification('MediaStreamDidFail', self, ndata)
            raise
        else:
            self.notification_center.post_notification('MediaStreamDidInitialize', self)

    def start(self, local_sdp, remote_sdp, stream_index):
        context = 'sdp_negotiation'
        try:
            remote_media = remote_sdp.media[stream_index]
            media_attributes = dict((attr.name, attr.value) for attr in remote_media.attributes)
            remote_accept_types = media_attributes.get('accept-types')
            # TODO: update accept_types and accept_wrapped_types from remote_media
            # TODO: chatroom, recvonly/sendonly?
            self.cpim_enabled = contains_mime_type(self.accept_types, 'message/cpim')
            self.private_messages_allowed = self.cpim_enabled # and isfocus and 'private-messages' in chatroom
            remote_uri_path = media_attributes.get('path')
            if remote_uri_path is None:
                raise AttributeError("remote SDP media does not have 'path' attribute")
            full_remote_path = [parse_uri(uri) for uri in remote_uri_path.split()]
            context = 'start'
            self.msrp = self.msrp_connector.complete(full_remote_path)
            self.session = MSRPSession(self.msrp, accept_types=self.accept_types, on_incoming_cb=self._on_incoming)
            self.msrp_connector = None
            self._on_start()
        except Exception, ex:
            ndata = NotificationData(context=context, failure=Failure(), reason=str(ex) or type(ex).__name__)
            self.notification_center.post_notification('MediaStreamDidFail', self, ndata)
            raise
        else:
            self.notification_center.post_notification('MediaStreamDidStart', self)
            for send_args in self.message_queue:
                proc.spawn_greenlet(self._send_raw_message, *send_args)
            self.message_queue.clear()
        # what if starting has failed? should I generate MSRPChatDidNotDeliver per each message?

    def _on_start(self):
        pass

    def end(self):
        if self.session is None and self.msrp_connector is None:
            return
        session, self.session = self.session, None
        msrp_connector, self.msrp_connector = self.msrp_connector, None
        self.notification_center.post_notification('MediaStreamWillEnd', self)
        try:
            if session is not None:
                session.shutdown()
            if msrp_connector is not None:
                msrp_connector.cleanup()
        finally:
            self.notification_center.post_notification('MediaStreamDidEnd', self)

    def _on_incoming(self, chunk=None, error=None):
        if error is not None:
            if isinstance(error.value, ConnectionDone):
                self.notification_center.post_notification('MediaStreamDidEnd', self)
            else:
                ndata = NotificationData(context='reading', failure=error, reason=error.getErrorMessage())
                self.notification_center.post_notification('MediaStreamDidFail', self, ndata)
        elif chunk.method=='REPORT':
            # in theory, REPORT can come with Byte-Range which would limit the scope of the REPORT to the part of the message.
            data = NotificationData(message_id=chunk.message_id, message=chunk, code=chunk.status.code, reason=chunk.status.comment)
            if chunk.status.code == 200:
                self.notification_center.post_notification('MSRPChatDidDeliverMessage', self, data)
            else:
                self.notification_center.post_notification('MSRPChatDidNotDeliverMessage', self, data)
        elif chunk.method=='SEND':
            if self.direction=='sendonly':
                return
            if chunk.content_type.lower()=='message/cpim':
                cpim_headers, content = MessageCPIMParser.parse_string(chunk.data)
                content_type = cpim_headers.get('Content-Type')
            else:
                cpim_headers = {}
                content = chunk.data
                content_type = chunk.content_type
            # Note: success reports are issued by msrplib
            # TODO: check wrapped content-type and issue a report if it's invalid
            ndata = NotificationData(content=content, content_type=content_type, cpim_headers=cpim_headers, message=chunk)
            self.notification_center.post_notification('MSRPChatGotMessage', self, ndata)

    def _on_transaction_response(self, message_id, response):
        if response.code!=200:
            data = NotificationData(message_id=message_id, message=response, code=response.code, reason=response.comment)
            self.notification_center.post_notification('MSRPChatDidNotDeliverMessage', self, data)

    def _send_raw_message(self, message, content_type, failure_report=None, success_report=None):
        """Send raw MSRP message. For IM prefer send_message.
        If called before the connection was established, the messages will be
        queued until MediaStreamDidStart notification.

        Return generated MSRP chunk (MSRPData); to get Message-ID use its 'message_id' attribute.
        """
        if self.direction=='recvonly':
            raise MSRPChatError('Cannot send message on recvonly stream')
        if self.session is None:
            self.message_queue.append((message, content_type, failure_report, success_report))
            return
        if not contains_mime_type(self.accept_types, content_type):
            raise MSRPChatError('Invalid content_type for outgoing message: %r' % (content_type, ))
        message_id = '%x' % random.getrandbits(64)
        chunk = self.session.make_message(message, content_type=content_type, message_id=message_id)
        if failure_report is not None:
            chunk.add_header(FailureReportHeader(failure_report))
        if success_report is not None:
            chunk.add_header(SuccessReportHeader(success_report))
        self.session.send_chunk(chunk, response_cb=lambda response: self._on_transaction_response(message_id, response))
        return chunk

    def send_message(self, content, content_type='text/plain', to_uri=None, dt=None):
        """Send IM message. Prefer Message/CPIM wrapper if it is supported.
        If called before the connection was established, the messages will be
        queued until MediaStreamDidStart notification.

        - content (str) - content of the message;
        - to_uri (SIPURI) - "To" header of CPIM wrapper;
          if None, use the default supplied in __init__
          'to_uri' may only differ from the one supplied in __init__ if the remote
          party supports private messages. If it does not, MSRPChatError will be raised;
        - content_type (str) - Content-Type of wrapped message;
          (Content-Type of MSRP message is always Message/CPIM in that case)
          If Message/CPIM is not supported, Content-Type of MSRP message.

        Return generated MSRP chunk (MSRPData); to get Message-ID use its 'message_id' attribute.

        These MSRP headers are used to enable end-to-end success reports and
        to disable hop-to-hop successful responses:
        Failure-Report: partial
        Success-Report: yes
        """
        if self.cpim_enabled:
            if to_uri is None:
                to_uri = self.remote_uri
            elif not self.private_messages_allowed and to_uri != self.remote_uri:
                raise MSRPChatError('The remote end does not support private messages')
            if dt is None:
                dt = datetime.utcnow()
            msg = MessageCPIM(content, content_type, from_=self.from_uri, to=to_uri, datetime=dt)
            return self._send_raw_message(str(msg), 'message/cpim', failure_report='partial', success_report='yes')
        else:
            if to_uri is not None and to_uri != self.remote_uri:
                raise MSRPChatError('Private messages are not available, because CPIM wrapper is not used')
            return self._send_raw_message(content, content_type)

    def send_file(self, outgoing_file):
        self.session.send_file(outgoing_file)


class MSRPOutgoingFileStream(MSRPChat):

    def __init__(self, account, filename, fileobj, size, content_type, sha1):
        file_selector = FileSelector(os.path.basename(filename), content_type, size, sha1)
        MSRPChat.__init__(self, account, direction='sendonly', file_selector=file_selector)
        self.outgoing_file = OutgoingFile(fileobj, size, content_type=content_type)
        self.outgoing_file.headers['Success-Report'] = SuccessReportHeader('yes')
        self.outgoing_file.headers['Failure-Report'] = FailureReportHeader('partial')

    def _on_start(self):
        self.send_file(self.outgoing_file)


class MSRPIncomingFileStream(MSRPChat):

    def __init__(self, account):
        MSRPChat.__init__(self, account, direction='recvonly')

    def validate_incoming(self, remote_sdp, stream_index):
        media = remote_sdp.media[stream_index]
        media_attributes = dict((attr.name, attr.value) for attr in media.attributes)
        self.file_selector = FileSelector.parse(media_attributes['file-selector'])
        return True

