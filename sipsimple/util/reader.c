#include <stdlib.h>
#include <stdio.h>
#include <sys/stat.h> 
#include <fcntl.h>

#define THIS_FILE   "reader.c"

/* Open pipe for non-buffered writes */
int open_rpipe_port(char *path)
{
   //printf("PJReader opened for read file %s\n", path);
   return open(path, O_RDONLY);
}


/* Close pipe for non-buffered writes */
int close_rpipe_port(int fd)
{
   //printf("PJReader closed file\n");
   return close(fd);
}


/* Read samples from pipe */
int read_rpipe_port(int fd, char *samples, unsigned int count)
{
   int ret = read(fd, samples, count);
   //printf("PJReader read %d bytes\n", ret);
   return ret;
}

