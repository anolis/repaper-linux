#!/usr/bin/env python3
"""
Bridge the ISKN Repaper serial pen stream into a Linux uinput tablet.

This is an iteration tool, not the final kernel driver.  It initializes the
Repaper vendor serial stream, parses confirmed 0x04 pen2d packets, and exposes
a virtual absolute pen device that applications such as GIMP can select.
"""

import argparse
import fcntl
import os
import select
import struct
import sys
import termios
import time
from pathlib import Path

USB_VENDOR = '2c87'
USB_PRODUCT = '0001'
SERIAL_SPEED = getattr(termios, 'B4000000', termios.B115200)

SIGNATURE = bytes([0xb3, 0xa5, 0xe1])
FRAME_SIZES = {
    0x01: 8,
    0x02: 42,
    0x04: 15,
    0x09: 13,
    0x0a: 19,
    0x0f: 18,
    0x13: 42,
    0x14: 74,
    0x18: 20,
}

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0x00

BTN_TOOL_PEN = 0x140
BTN_TOUCH = 0x14a

ABS_X = 0x00
ABS_Y = 0x01
ABS_PRESSURE = 0x18

BUS_USB = 0x03

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567

DEFAULT_X_MIN = -9622
DEFAULT_X_MAX = 9777
DEFAULT_Y_MIN = -10402
DEFAULT_Y_MAX = 13018
DEFAULT_PRESSURE = 1024


def normalize_orientation(value):
    if value == 'landscape':
        return 'landscape-cw'
    return value


def usb_device_matches(path):
    try:
        vendor = (path / 'idVendor').read_text().strip().lower()
        product = (path / 'idProduct').read_text().strip().lower()
    except OSError:
        return False
    return vendor == USB_VENDOR and product == USB_PRODUCT


def find_serial():
    usb_root = Path('/sys/bus/usb/devices')
    if not usb_root.is_dir():
        return None

    for usb_dev in sorted(usb_root.iterdir()):
        if not usb_device_matches(usb_dev):
            continue
        for tty in sorted(usb_dev.glob('**/ttyACM*')):
            if tty.name.startswith('ttyACM'):
                return f'/dev/{tty.name}'
    return None


def set_modem_lines(fd):
    lines = struct.unpack('I', fcntl.ioctl(fd, termios.TIOCMGET,
                                          struct.pack('I', 0)))[0]
    lines |= termios.TIOCM_DTR | termios.TIOCM_RTS
    fcntl.ioctl(fd, termios.TIOCMSET, struct.pack('I', lines))


def open_serial(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = SERIAL_SPEED
    attrs[5] = SERIAL_SPEED
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    set_modem_lines(fd)
    return fd


def crc16_ccitt(data):
    crc = 0
    for byte in data:
        byte ^= (crc >> 8) & 0xff
        byte ^= byte >> 4
        crc = ((crc << 8) ^ (byte << 12) ^ (byte << 5) ^ byte) & 0xffff
    return crc


def iskn_packet(block_type, payload):
    payload = bytes(payload)
    crc = crc16_ccitt(payload)
    return SIGNATURE + bytes([block_type]) + payload + crc.to_bytes(2, 'little')


def init_stream(fd, subscribe_max):
    for request_id in range(6):
        os.write(fd, iskn_packet(0x34, [request_id]))
        time.sleep(0.02)

    for subscribe_id in range(subscribe_max + 1):
        os.write(fd, iskn_packet(0x33, subscribe_id.to_bytes(2, 'little')))
        time.sleep(0.01)


def iter_frames_from_buffer(buffer):
    while True:
        start = buffer.find(SIGNATURE)
        if start < 0:
            del buffer[:]
            return
        if start:
            del buffer[:start]
        if len(buffer) < 4:
            return

        pkt_type = buffer[3]
        frame_len = FRAME_SIZES.get(pkt_type)
        if frame_len is None:
            del buffer[:3]
            continue
        if len(buffer) < frame_len:
            return

        frame = bytes(buffer[:frame_len])
        del buffer[:frame_len]
        yield frame


def parse_pen2d(frame):
    payload = frame[4:-2]
    expected_crc = int.from_bytes(frame[-2:], 'little')
    actual_crc = crc16_ccitt(payload)
    if expected_crc != actual_crc or len(payload) != 9:
        return None

    x, y, rot_x, rot_y, state = struct.unpack('<hhhhB', payload)
    return {
        'x': x,
        'y': y,
        'rot_x': rot_x,
        'rot_y': rot_y,
        'touch': state != 0,
    }


def clamp(value, low, high):
    return max(low, min(high, value))


def write_event(fd, event_type, code, value):
    sec, frac = divmod(time.time_ns(), 1_000_000_000)
    usec = frac // 1000
    os.write(fd, struct.pack('llHHi', sec, usec, event_type, code, value))


def sync(fd):
    write_event(fd, EV_SYN, SYN_REPORT, 0)


def enable_bit(fd, request, bit):
    fcntl.ioctl(fd, request, bit)


def axis_bounds(args):
    orientation = normalize_orientation(args.orientation)
    if orientation == 'portrait':
        return args.x_min, args.x_max, args.y_min, args.y_max
    return args.y_min, args.y_max, args.x_min, args.x_max


def create_uinput(args):
    fd = os.open(args.uinput, os.O_WRONLY | os.O_NONBLOCK)

    for event_type in (EV_KEY, EV_ABS):
        enable_bit(fd, UI_SET_EVBIT, event_type)
    for key in (BTN_TOOL_PEN, BTN_TOUCH):
        enable_bit(fd, UI_SET_KEYBIT, key)
    for axis in (ABS_X, ABS_Y, ABS_PRESSURE):
        enable_bit(fd, UI_SET_ABSBIT, axis)

    absmax = [0] * 64
    absmin = [0] * 64
    absfuzz = [0] * 64
    absflat = [0] * 64
    x_min, x_max, y_min, y_max = axis_bounds(args)
    absmin[ABS_X] = x_min
    absmax[ABS_X] = x_max
    absmin[ABS_Y] = y_min
    absmax[ABS_Y] = y_max
    absmin[ABS_PRESSURE] = 0
    absmax[ABS_PRESSURE] = args.pressure

    name = args.name.encode('utf-8')[:79]
    user_dev = struct.pack(
        '80sHHHHI' + 'i' * 64 * 4,
        name,
        BUS_USB,
        int(USB_VENDOR, 16),
        int(USB_PRODUCT, 16),
        1,
        0,
        *(absmax + absmin + absfuzz + absflat),
    )
    os.write(fd, user_dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def transform_sample(sample, args):
    x = clamp(sample['x'], args.x_min, args.x_max)
    y = clamp(sample['y'], args.y_min, args.y_max)
    orientation = normalize_orientation(args.orientation)

    if orientation == 'portrait':
        return x, y
    if orientation == 'landscape-cw':
        return y, args.x_min + args.x_max - x
    if orientation == 'landscape-ccw':
        return args.y_min + args.y_max - y, x

    raise ValueError(f'unsupported orientation: {args.orientation}')


def emit_pen(fd, sample, args, previous_touch):
    x, y = transform_sample(sample, args)
    touch = sample['touch']

    write_event(fd, EV_KEY, BTN_TOOL_PEN, 1)
    write_event(fd, EV_KEY, BTN_TOUCH, int(touch))
    write_event(fd, EV_ABS, ABS_X, x)
    write_event(fd, EV_ABS, ABS_Y, y)
    write_event(fd, EV_ABS, ABS_PRESSURE, args.pressure if touch else 0)
    sync(fd)

    if previous_touch and not touch:
        write_event(fd, EV_KEY, BTN_TOUCH, 0)
        write_event(fd, EV_ABS, ABS_PRESSURE, 0)
        sync(fd)

    return touch


def main():
    parser = argparse.ArgumentParser(
        description='Expose the ISKN Repaper serial stream as a uinput tablet.',
    )
    parser.add_argument('--serial', help='serial node, e.g. /dev/ttyACM0')
    parser.add_argument('--uinput', default='/dev/uinput',
                        help='uinput node to create the virtual tablet')
    parser.add_argument('--name', default='ISKN Repaper Virtual Tablet',
                        help='virtual input device name')
    parser.add_argument('--x-min', type=int, default=DEFAULT_X_MIN)
    parser.add_argument('--x-max', type=int, default=DEFAULT_X_MAX)
    parser.add_argument('--y-min', type=int, default=DEFAULT_Y_MIN)
    parser.add_argument('--y-max', type=int, default=DEFAULT_Y_MAX)
    parser.add_argument('--orientation', default='portrait',
                        choices=('portrait', 'landscape', 'landscape-cw',
                                 'landscape-ccw'),
                        help='tablet orientation; landscape aliases landscape-cw')
    parser.add_argument('--pressure', type=int, default=DEFAULT_PRESSURE)
    parser.add_argument('--subscribe-max', type=int, default=5,
                        help='highest vendor auto-register id to subscribe')
    parser.add_argument('--verbose', action='store_true',
                        help='print decoded pen samples')
    args = parser.parse_args()

    serial = args.serial or find_serial()
    if not serial:
        sys.exit('Cannot find Repaper serial node. Is the tablet plugged in?')

    ser_fd = None
    ui_fd = None
    try:
        ser_fd = open_serial(serial)
        ui_fd = create_uinput(args)
        print(f'Serial: {serial}')
        print(f'Uinput: {args.name}')
        print(f'Orientation: {normalize_orientation(args.orientation)}')
        print('Listening; press Ctrl-C to stop.')

        init_stream(ser_fd, args.subscribe_max)
        buffer = bytearray()
        previous_touch = False

        while True:
            readable, _, _ = select.select([ser_fd], [], [], 1.0)
            if not readable:
                continue
            chunk = os.read(ser_fd, 256)
            if not chunk:
                continue
            buffer.extend(chunk)

            for frame in iter_frames_from_buffer(buffer):
                if frame[3] != 0x04:
                    continue
                sample = parse_pen2d(frame)
                if sample is None:
                    continue
                previous_touch = emit_pen(ui_fd, sample, args, previous_touch)
                if args.verbose:
                    out_x, out_y = transform_sample(sample, args)
                    print(
                        f'raw_x={sample["x"]} raw_y={sample["y"]} '
                        f'out_x={out_x} out_y={out_y} '
                        f'touch={sample["touch"]}',
                        flush=True,
                    )
    except KeyboardInterrupt:
        pass
    finally:
        if ui_fd is not None:
            try:
                write_event(ui_fd, EV_KEY, BTN_TOUCH, 0)
                write_event(ui_fd, EV_KEY, BTN_TOOL_PEN, 0)
                write_event(ui_fd, EV_ABS, ABS_PRESSURE, 0)
                sync(ui_fd)
                fcntl.ioctl(ui_fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(ui_fd)
        if ser_fd is not None:
            os.close(ser_fd)


if __name__ == '__main__':
    main()
