#include <stdlib.h>
#include <stdio.h>
#include <sys/stat.h> 
#include <fcntl.h>

#define THIS_FILE   "writer.c"

/* EXPORTED: Open pipe for non-buffered writes */
__attribute__ ((visibility ("default")))
int open_pipe_port(char *path)
{
   printf("PJWriter opened for write %s\n", path);
   return open(path, O_WRONLY | O_DSYNC);
}


/* EXPORTED: Close pipe for non-buffered writes */
__attribute__ ((visibility ("default")))
int close_pipe_port(int fd)
{
   printf("PJWriter closed file \n");
   return close(fd);
}


/* EXPORTED: Writes samples into pipe without caching */
__attribute__ ((visibility ("default")))
int write_pipe_port(int fd, char *samples, unsigned int count)
{
   int ret = write(fd, samples, count);
   fsync(fd);
   printf("PJWriter wrote %d bytes\n", ret);
   return ret;
}

