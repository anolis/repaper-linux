#!/usr/bin/env python3
"""
Read Linux input events from the ISKN Repaper evdev node.

This checks the path used by drawing applications after hid-iskn has claimed
the tablet and marked it as direct input.
"""

import argparse
import os
import select
import struct
import sys
import time
from pathlib import Path

USB_VENDOR = '2c87'
USB_PRODUCT = '0001'
EVENT_STRUCT = struct.Struct('llHHi')

EV_TYPES = {
    0x00: 'EV_SYN',
    0x01: 'EV_KEY',
    0x02: 'EV_REL',
    0x03: 'EV_ABS',
    0x04: 'EV_MSC',
}

ABS_PRESSURE = 0x18

ABS_CODES = {
    0x00: 'ABS_X',
    0x01: 'ABS_Y',
    ABS_PRESSURE: 'ABS_PRESSURE',
    0x19: 'ABS_DISTANCE',
    0x1a: 'ABS_TILT_X',
    0x1b: 'ABS_TILT_Y',
    0x28: 'ABS_MISC',
}

REL_CODES = {
    0x00: 'REL_X',
    0x01: 'REL_Y',
}

KEY_CODES = {
    0x110: 'BTN_LEFT',
    0x111: 'BTN_RIGHT',
    0x112: 'BTN_MIDDLE',
    0x140: 'BTN_TOOL_PEN',
    0x141: 'BTN_TOOL_RUBBER',
    0x142: 'BTN_TOOL_BRUSH',
    0x143: 'BTN_TOOL_PENCIL',
    0x144: 'BTN_TOOL_AIRBRUSH',
    0x145: 'BTN_TOOL_FINGER',
    0x146: 'BTN_TOOL_MOUSE',
    0x147: 'BTN_TOOL_LENS',
    0x14a: 'BTN_TOUCH',
    0x14b: 'BTN_STYLUS',
    0x14c: 'BTN_STYLUS2',
}


def has_abs_pressure(input_dir):
    """True if this input node is the pen rather than the mouse sub-device.

    Same test hid-iskn.c uses to tell the two apart: only the digitizer
    reports ABS_PRESSURE.  The capabilities file is a big-endian list of
    64-bit hex words.
    """
    try:
        text = (input_dir / 'capabilities' / 'abs').read_text().strip()
    except OSError:
        return False

    bitmap = 0
    for word in text.split():
        bitmap = (bitmap << 64) | int(word, 16)
    return bool(bitmap & (1 << ABS_PRESSURE))


def find_event_node():
    patterns = [
        f'/sys/bus/hid/devices/*:{USB_VENDOR.upper()}:{USB_PRODUCT.upper()}.*',
        f'/sys/bus/hid/devices/*:{USB_VENDOR}:{USB_PRODUCT}.*',
    ]

    fallback = None
    for pattern in patterns:
        for hid_dev in sorted(Path('/').glob(pattern.lstrip('/'))):
            for event in sorted(hid_dev.glob('input/input*/event*')):
                if not event.name.startswith('event'):
                    continue
                node = f'/dev/input/{event.name}'
                # The device exposes a mouse sub-device that sorts first;
                # picking it means watching an interface the pen never uses.
                if has_abs_pressure(event.parent):
                    return node
                if fallback is None:
                    fallback = node

    return fallback


def code_name(event_type, code):
    if event_type == 0x03:
        return ABS_CODES.get(code, f'ABS_{code}')
    if event_type == 0x02:
        return REL_CODES.get(code, f'REL_{code}')
    if event_type == 0x01:
        return KEY_CODES.get(code, f'KEY_{code}')
    return str(code)


def main():
    parser = argparse.ArgumentParser(description='Monitor Repaper evdev events.')
    parser.add_argument('--event', help='event node to use, e.g. /dev/input/event24')
    parser.add_argument('--timeout', type=float, default=10.0,
                        help='seconds to wait for events')
    args = parser.parse_args()

    event_node = args.event or find_event_node()
    if not event_node:
        sys.exit('Cannot find Repaper event node. Is the tablet plugged in?')

    print(f'Event: {event_node}')
    print(f'Listening for {args.timeout}s; move the pen and press on the surface.')

    try:
        fd = os.open(event_node, os.O_RDONLY | os.O_NONBLOCK)
    except PermissionError:
        sys.exit(f'Cannot open {event_node}; run with sudo or add a udev rule')
    except OSError as e:
        sys.exit(f'Cannot open {event_node}: {e}')

    got = False
    deadline = time.time() + args.timeout
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                continue

            while True:
                try:
                    data = os.read(fd, EVENT_STRUCT.size)
                except BlockingIOError:
                    break
                if len(data) != EVENT_STRUCT.size:
                    break

                sec, usec, event_type, code, value = EVENT_STRUCT.unpack(data)
                type_name = EV_TYPES.get(event_type, f'EV_{event_type}')
                print(f'{sec}.{usec:06d} {type_name:<6} '
                      f'{code_name(event_type, code):<16} {value}')
                got = True
    finally:
        os.close(fd)

    if not got:
        print('No evdev events received.')


if __name__ == '__main__':
    main()
