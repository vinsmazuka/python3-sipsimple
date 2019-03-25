# cython: language_level=2

import sys

cdef extern from "reader.c" nogil:
    int open_rpipe_port(char *path)
    int close_rpipe_port(int fd)
    int read_rpipe_port(int fd, void *samples, unsigned int count)

cdef class PJReader:
    cdef int fd

    def __init__(self):
        self.fd = -1;

    cpdef int open(self, char *path):
        with nogil:
            self.fd = open_rpipe_port(path)
            return self.fd

    cpdef int close(self):
        with nogil:
            return close_rpipe_port(self.fd)

    cpdef int read(self, char *data, int len):
        with nogil:
            return read_rpipe_port(self.fd, data, len)
