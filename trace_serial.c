// SPDX-License-Identifier: MIT
/*
 * LD_PRELOAD serial tracer for libISKN_API.so experiments.
 */

#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

static int (*real_open)(const char *pathname, int flags, ...);
static ssize_t (*real_read)(int fd, void *buf, size_t count);
static ssize_t (*real_write)(int fd, const void *buf, size_t count);
static int (*real_ioctl)(int fd, unsigned long request, ...);
static int traced_fd = -1;

static void init_real(void)
{
	if (!real_open)
		real_open = dlsym(RTLD_NEXT, "open");
	if (!real_read)
		real_read = dlsym(RTLD_NEXT, "read");
	if (!real_write)
		real_write = dlsym(RTLD_NEXT, "write");
	if (!real_ioctl)
		real_ioctl = dlsym(RTLD_NEXT, "ioctl");
}

static bool is_serial_path(const char *path)
{
	return path && strstr(path, "/dev/ttyACM") == path;
}

static void dump_bytes(const char *label, const void *buf, ssize_t len)
{
	const unsigned char *bytes = buf;

	fprintf(stderr, "[trace] %s %zd:", label, len);
	for (ssize_t i = 0; i < len; ++i)
		fprintf(stderr, " %02x", bytes[i]);
	fprintf(stderr, "\n");
}

int open(const char *pathname, int flags, ...)
{
	mode_t mode = 0;
	int ret;

	init_real();
	if (flags & O_CREAT) {
		va_list ap;
		va_start(ap, flags);
		mode = va_arg(ap, int);
		va_end(ap);
		ret = real_open(pathname, flags, mode);
	} else {
		ret = real_open(pathname, flags);
	}

	if (is_serial_path(pathname)) {
		traced_fd = ret;
		fprintf(stderr, "[trace] open %s flags=0x%x -> %d errno=%d\n",
			pathname, flags, ret, errno);
	}

	return ret;
}

ssize_t read(int fd, void *buf, size_t count)
{
	ssize_t ret;

	init_real();
	ret = real_read(fd, buf, count);
	if (fd == traced_fd && ret > 0)
		dump_bytes("read", buf, ret);
	return ret;
}

ssize_t write(int fd, const void *buf, size_t count)
{
	ssize_t ret;

	init_real();
	if (fd == traced_fd)
		dump_bytes("write", buf, count);
	ret = real_write(fd, buf, count);
	if (fd == traced_fd)
		fprintf(stderr, "[trace] write -> %zd errno=%d\n", ret, errno);
	return ret;
}

int ioctl(int fd, unsigned long request, ...)
{
	void *arg = NULL;
	int ret;
	va_list ap;

	init_real();
	va_start(ap, request);
	arg = va_arg(ap, void *);
	va_end(ap);

	ret = real_ioctl(fd, request, arg);
	if (fd == traced_fd)
		fprintf(stderr, "[trace] ioctl req=0x%lx arg=%p -> %d errno=%d\n",
			request, arg, ret, errno);
	return ret;
}
