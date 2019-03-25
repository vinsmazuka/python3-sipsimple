#include <stdlib.h>
#include <stdio.h>
#include <sys/stat.h> 
#include <fcntl.h>

#define THIS_FILE   "reader.c"

/* EXPORTED: Open pipe for non-buffered writes */
__attribute__ ((visibility ("default")))
int open_rpipe_port(char *path)
{
   return open(path, O_RDONLY | O_DSYNC);
}


/* EXPORTED: Close pipe for non-buffered writes */
__attribute__ ((visibility ("default")))
int close_rpipe_port(int fd)
{
   return close(fd);
}


/* EXPORTED: Writes samples into pipe without caching */
__attribute__ ((visibility ("default")))
int read_rpipe_port(int fd, char *samples, unsigned int count)
{
   int ret = read(fd, samples, count);
   return ret;
}

