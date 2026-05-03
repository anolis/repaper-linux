#!/usr/bin/env python3
"""
Probe tool for ISKN Repaper (2c87:0001).

Listens on both the HID raw interface and the CDC-ACM serial port, then
tries a handful of known-style init sequences and prints whatever the
device sends back.  Run with sudo.
"""

import argparse
import os
import select
import sys
import termios
import time
from pathlib import Path

USB_VENDOR = '2c87'
USB_PRODUCT = '0001'
TIMEOUT = 3.0   # seconds to wait for spontaneous data per phase
SERIAL_SPEED = getattr(termios, 'B4000000', termios.B115200)


def find_hidraw():
    """Return the hidraw node for the Repaper HID interface."""
    patterns = [
        f'/sys/bus/hid/devices/*:{USB_VENDOR.upper()}:{USB_PRODUCT.upper()}.*',
        f'/sys/bus/hid/devices/*:{USB_VENDOR}:{USB_PRODUCT}.*',
    ]

    for pattern in patterns:
        for hid_dev in sorted(Path('/').glob(pattern.lstrip('/'))):
            hidraw_dir = hid_dev / 'hidraw'
            if not hidraw_dir.is_dir():
                continue
            for child in sorted(hidraw_dir.iterdir()):
                if child.name.startswith('hidraw'):
                    return f'/dev/{child.name}'

    return None


def usb_device_matches(path):
    try:
        vendor = (path / 'idVendor').read_text().strip().lower()
        product = (path / 'idProduct').read_text().strip().lower()
    except OSError:
        return False
    return vendor == USB_VENDOR and product == USB_PRODUCT


def find_serial():
    """Return a ttyACM node attached to the Repaper USB device, if present."""
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

def hexdump(data, label=''):
    if label:
        print(f'  [{label}]', end=' ')
    print(' '.join(f'{b:02x}' for b in data))


def crc16_ccitt(data):
    crc = 0
    for byte in data:
        byte ^= (crc >> 8) & 0xff
        byte ^= byte >> 4
        crc = ((crc << 8) ^ (byte << 12) ^ (byte << 5) ^ byte) & 0xffff
    return crc


def iskn_packet(block_type, payload):
    body = bytes([block_type]) + bytes(payload)
    crc = crc16_ccitt(body)
    return bytes([0xb3, 0xa5, 0xe1]) + body + crc.to_bytes(2, 'little')


def read_short(fd_names, secs=0.3):
    got = False
    readable, _, _ = select.select(list(fd_names), [], [], secs)
    for fd in readable:
        try:
            data = os.read(fd, 256)
        except OSError:
            continue
        hexdump(data, fd_names[fd])
        got = True
    return got


def drain(fd_names, secs, label):
    """Read everything available from fds for `secs` seconds."""
    got = False
    deadline = time.time() + secs
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        r, _, _ = select.select(list(fd_names), [], [], remaining)
        for fd in r:
            try:
                data = os.read(fd, 64)
                name = fd_names[fd]
                hexdump(data, f'{label} {name}')
                got = True
            except OSError:
                pass
    return got


parser = argparse.ArgumentParser(description='Probe an ISKN Repaper tablet.')
parser.add_argument('--hidraw', help='hidraw node to use, e.g. /dev/hidraw9')
parser.add_argument('--serial', help='serial node to use, e.g. /dev/ttyACM0')
parser.add_argument('--timeout', type=float, default=TIMEOUT,
                    help='seconds to listen in passive phases')
args = parser.parse_args()

hidraw = args.hidraw or find_hidraw()
serial = args.serial or find_serial()

if not hidraw:
    sys.exit('Cannot find Repaper hidraw node. Is the tablet plugged in?')

print(f'HID raw: {hidraw}')
print(f'Serial:  {serial or "(not found)"}')

# -- open interfaces ----------------------------------------------------------
try:
    hid_fd = os.open(hidraw, os.O_RDWR | os.O_NONBLOCK)
except PermissionError:
    sys.exit(f'Cannot open {hidraw}; run with sudo or add a udev rule')
except OSError as e:
    sys.exit(f'Cannot open {hidraw}: {e}')

ser_fd = None
if serial:
    try:
        ser_fd = os.open(serial, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        # Configure the serial side like the vendor library: raw 8N1 at 4 Mbps.
        attrs = termios.tcgetattr(ser_fd)
        attrs[0] = 0            # iflag: no input processing
        attrs[1] = 0            # oflag: no output processing
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        attrs[3] = 0            # lflag: raw
        attrs[4] = SERIAL_SPEED
        attrs[5] = SERIAL_SPEED
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 1
        termios.tcsetattr(ser_fd, termios.TCSANOW, attrs)
    except OSError as e:
        print(f'Cannot open {serial}: {e}')
        ser_fd = None

fd_names = {hid_fd: 'HID'}
if ser_fd is not None:
    fd_names[ser_fd] = 'SER'

# -- phase 1: just listen -----------------------------------------------------
print(f'\n=== Phase 1: passive listen {args.timeout}s (move pen on tablet) ===')
got = drain(fd_names, args.timeout, 'passive')
if not got:
    print('  (no data — device is silent without init)')

# -- phase 2: try HID output reports -----------------------------------------
# Report ID 4 is the 63-byte vendor output.  Try a few plausible commands.
# Byte 0 = report ID, bytes 1..63 = payload (padded with 0x00).
hid_cmds = [
    (0x01, 'get-status'),
    (0x02, 'start-stream'),
    (0x10, 'cmd-0x10'),
    (0x20, 'cmd-0x20'),
    (0x30, 'cmd-0x30'),
]

print(f'\n=== Phase 2: HID output report (ID 4) probes ===')
for cmd_byte, name in hid_cmds:
    pkt = bytes([0x04, cmd_byte]) + bytes(62)   # report ID 4, 63 payload bytes
    try:
        os.write(hid_fd, pkt)
    except OSError as e:
        print(f'  write {name}: {e}')
        continue
    print(f'  sent cmd=0x{cmd_byte:02x} ({name})', end=' → ')
    sys.stdout.flush()
    r, _, _ = select.select(list(fd_names), [], [], 0.3)
    if r:
        for fd in r:
            data = os.read(fd, 64)
            hexdump(data, fd_names[fd])
    else:
        print('no response')

# -- phase 3: try the vendor serial protocol shape ----------------------------
if ser_fd is not None:
    print(f'\n=== Phase 3: ISKN serial protocol probes ===')
    # Disassembled from libISKN_API.so:
    #   request:   b3 a5 e1 34 <request-id> <crc16-le>
    #   subscribe: b3 a5 e1 33 <auto-id-le16> <crc16-le>
    request_ids = [
        (0x01, 'request-1'),
        (0x02, 'request-2'),
        (0x03, 'request-3'),
        (0x04, 'request-4'),
        (0x05, 'request-5'),
    ]
    subscribe_ids = [
        (0x0001, 'subscribe-1'),
        (0x0002, 'subscribe-2'),
        (0x0003, 'subscribe-3'),
        (0x0004, 'subscribe-4'),
        (0x0005, 'subscribe-5'),
    ]

    for request_id, name in request_ids:
        pkt = iskn_packet(0x34, [request_id])
        try:
            os.write(ser_fd, pkt)
        except OSError as e:
            print(f'  write {name}: {e}')
            continue
        print(f'  sent {name} {pkt.hex()}', end=' → ')
        sys.stdout.flush()
        if not read_short(fd_names):
            print('no response')

    for subscribe_id, name in subscribe_ids:
        pkt = iskn_packet(0x33, subscribe_id.to_bytes(2, 'little'))
        try:
            os.write(ser_fd, pkt)
        except OSError as e:
            print(f'  write {name}: {e}')
            continue
        print(f'  sent {name} {pkt.hex()}', end=' → ')
        sys.stdout.flush()
        if not read_short(fd_names):
            print('no response')

    print('  listening after subscriptions; move pen on tablet')
    drain(fd_names, args.timeout, 'serial-subscribe')

# -- phase 4: passive listen again after probes -------------------------------
print(f'\n=== Phase 4: listen {args.timeout}s after probes (move pen) ===')
drain(fd_names, args.timeout, 'post-probe')

os.close(hid_fd)
if ser_fd is not None:
    os.close(ser_fd)
print('\nDone.')
