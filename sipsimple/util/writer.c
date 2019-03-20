#include <stdlib.h>
#include <stdio.h>
#include <sys/stat.h> 
#include <fcntl.h>

#define THIS_FILE   "pjs_pipe_port.c"

/* EXPORTED: Open pipe for non-buffered writes */
__attribute__ ((visibility ("default")))
int open_pipe_port(char *path)
{
   return open(path, O_WRONLY | O_DSYNC);
}


/* EXPORTED: Close pipe for non-buffered writes */
__attribute__ ((visibility ("default")))
int close_pipe_port(int fd)
{
   return close(fd);
}


/* EXPORTED: Writes samples into pipe without caching */
__attribute__ ((visibility ("default")))
int write_pipe_port(int fd, char *samples, unsigned int count)
{
   int ret = write(fd, samples, count);
   fsync(fd);
   return ret;
}

