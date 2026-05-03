obj-m += hid-iskn.o

KDIR := /lib/modules/$(shell uname -r)/build
CXX ?= g++
CC ?= gcc
CXXFLAGS ?= -Wall -Wextra -O2
CFLAGS ?= -Wall -Wextra -O2

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

tools: iskn_harness trace_serial.so

iskn_harness: iskn_harness.cpp
	$(CXX) $(CXXFLAGS) -o $@ $< -ldl

trace_serial.so: trace_serial.c
	$(CC) $(CFLAGS) -fPIC -shared -o $@ $< -ldl

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
	rm -f iskn_harness trace_serial.so
	rm -rf __pycache__
