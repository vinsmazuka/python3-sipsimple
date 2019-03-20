
import sys

cdef extern from "mediaport.c":
    int pjmedia_pipe_player_port_create(pj_pool_t *pool, const char *filename, 
                                      unsigned int sampling_rate, int channel_count, 
                                      pjmedia_port **p_port, int alaw) nogil
    int pjmedia_pipe_player_port_destroy(pjmedia_port *p_port) nogil
    int pjmedia_pipe_player_set_eof_cb(pjmedia_port *port, void *user_data,
                                      int cb(pjmedia_port *port, void *usr_data) with gil) nogil


cdef class PipeFile:
    cdef object __weakref__
    cdef object weakref
    cdef int _slot
    cdef int _volume
    cdef pj_mutex_t *_lock
    cdef pj_pool_t *_pool
    cdef pjmedia_port *_port
    cdef readonly str filename
    cdef readonly AudioMixer mixer
    cdef readonly int rate
    cdef readonly int channels
    cdef readonly int alaw

    def __cinit__(self, *args, **kwargs):
        cdef int status

        self.weakref = weakref.ref(self)
        Py_INCREF(self.weakref)

        status = pj_mutex_create_recursive(_get_ua()._pjsip_endpoint._pool, "pipe_file_lock", &self._lock)
        if status != 0:
            raise PJSIPError("failed to create lock", status)

        self._slot = -1
        self._volume = 100

    def __init__(self, AudioMixer mixer, filename, sampling_rate, channels, alaw):
        if self.filename is not None:
            raise SIPCoreError("PipeFile.__init__() was already called")
        if mixer is None:
            raise ValueError("mixer argument may not be None")
        if filename is None:
            raise ValueError("pipename argument may not be None")
        if not isinstance(filename, basestring):
            raise TypeError("pipe argument must be str or unicode")
        if isinstance(filename, unicode):
            filename = filename.encode(sys.getfilesystemencoding())
        self.mixer = mixer
        self.filename = filename
        self.rate = sampling_rate
        self.channels = channels
        self.alaw = alaw

    cdef PJSIPUA _check_ua(self):
        cdef PJSIPUA ua
        try:
            ua = _get_ua()
            return ua
        except:
            self._pool = NULL
            self._port = NULL
            self._slot = -1
            return None

    property is_active:

        def __get__(self):
            self._check_ua()
            return self._port != NULL

    property slot:

        def __get__(self):
            self._check_ua()
            if self._slot == -1:
                return None
            else:
                return self._slot

    property volume:

        def __get__(self):
            return self._volume

        def __set__(self, value):
            cdef int slot
            cdef int status
            cdef int volume
            cdef pj_mutex_t *lock = self._lock
            cdef pjmedia_conf *conf_bridge
            cdef PJSIPUA ua

            ua = self._check_ua()

            if ua is not None:
                with nogil:
                    status = pj_mutex_lock(lock)
                if status != 0:
                    raise PJSIPError("failed to acquire lock", status)
            try:
                conf_bridge = self.mixer._obj
                slot = self._slot

                if value < 0:
                    raise ValueError("volume attribute cannot be negative")
                if ua is not None and self._slot != -1:
                    volume = int(value * 1.28 - 128)
                    with nogil:
                        status = pjmedia_conf_adjust_rx_level(conf_bridge, slot, volume)
                    if status != 0:
                        raise PJSIPError("Could not set volume of .pipe file", status)
                self._volume = value
            finally:
                if ua is not None:
                    with nogil:
                        pj_mutex_unlock(lock)

    def start(self):
        cdef char *filename
        cdef int status
        cdef void *weakref
        cdef pj_pool_t *pool
        cdef pj_mutex_t *lock = self._lock
        cdef pjmedia_port **port_address
        cdef bytes pool_name
        cdef char* c_pool_name
        cdef PJSIPUA ua

        ua = _get_ua()

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("Failed to acquire lock", status)
        try:
            filename = PyString_AsString(self.filename)
            port_address = &self._port
            weakref = <void *> self.weakref

            if self._port != NULL:
                raise SIPCoreError("Pipe file is already playing")
            pool_name = b"PipeFile_%d" % id(self)
            pool = ua.create_memory_pool(pool_name, 4096, 4096)
            self._pool = pool
            try:
                with nogil:
                    # Create media port for pipe (TODO: move sampling rate to class init)
                    # buffer = 0 == disabled!
                    status = pjmedia_pipe_player_port_create(pool, filename, self.rate, 
                                                             self.channels, port_address, self.alaw)

                if status != 0:
                    raise PJSIPError("Could not open pipe file", status)
                with nogil:
                    # wav cb setter is ok for pipe
                    status = pjmedia_pipe_player_set_eof_cb(self._port, weakref, cb_play_pipe_eof)
                if status != 0:
                    raise PJSIPError("Could not set pipe EOF callback", status)
                self._slot = self.mixer._add_port(ua, self._pool, self._port)
                if self._volume != 100:
                    self.volume = self._volume
            except:
                print("Exception during port create!")
                self._stop(ua, 0)
                raise
        finally:
            with nogil:
                pj_mutex_unlock(lock)

    cdef int _stop(self, PJSIPUA ua, int notify) except -1:
        cdef int status
        cdef int was_active
        cdef pj_pool_t *pool
        cdef pjmedia_port *port

        print("Pipe port _stop() called!")
        port = self._port
        was_active = 0

        if self._slot != -1:
            was_active = 1
            self.mixer._remove_port(ua, self._slot)
            self._slot = -1
        if self._port != NULL:
            with nogil:
		# close file and destroy port
                pjmedia_pipe_player_port_destroy(port)
            self._port = NULL
            was_active = 1
        ua.release_memory_pool(self._pool)
        self._pool = NULL
        if notify and was_active:
            _add_event("PipeFileDidFinishPlaying", dict(obj=self))

    def stop(self):
        cdef int status
        cdef pj_mutex_t *lock = self._lock
        cdef PJSIPUA ua

        ua = self._check_ua()
        if ua is None:
            return

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            self._stop(ua, 1)
        finally:
            with nogil:
                pj_mutex_unlock(lock)

    def __dealloc__(self):
        cdef PJSIPUA ua
        cdef Timer timer
        try:
            ua = _get_ua()
        except:
            return
        self._stop(ua, 0)
        timer = Timer()
        try:
            timer.schedule(60, deallocate_weakref, self.weakref)
        except SIPCoreError:
            pass
        if self._lock != NULL:
            pj_mutex_destroy(self._lock)

    cdef int _cb_eof(self, timer) except -1:
        cdef int status
        cdef pj_mutex_t *lock = self._lock
        cdef PJSIPUA ua

        ua = self._check_ua()
        if ua is None:
            return 0

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            self._stop(ua, 1)
        finally:
            with nogil:
                pj_mutex_unlock(lock)


cdef int cb_play_pipe_eof(pjmedia_port *port, void *user_data) with gil:
    cdef Timer timer
    cdef PipeFile pipe_file

    print("Pipe eof handler called")
    pipe_file = (<object> user_data)()
    if pipe_file is not None:
        timer = Timer()
        timer.schedule(0, <timer_callback>pipe_file._cb_eof, pipe_file)
    # do not return PJ_SUCCESS because if you do pjsip will access the just deallocated port
    return 1

