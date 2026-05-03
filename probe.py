#!/usr/bin/env python3
"""
Probe tool for ISKN Repaper (2c87:0001).

Listens on both the HID raw interface and the CDC-ACM serial port, then
tries a handful of known-style init sequences and prints whatever the
device sends back.  Run with sudo.
"""

import os, sys, select, time, struct, termios, tty, fcntl

HIDRAW  = '/dev/hidraw9'
SERIAL  = '/dev/ttyACM0'
TIMEOUT = 3.0   # seconds to wait for spontaneous data per phase

def hexdump(data, label=''):
    if label:
        print(f'  [{label}]', end=' ')
    print(' '.join(f'{b:02x}' for b in data))

def drain(fds, secs, label):
    """Read everything available from fds for `secs` seconds."""
    got = False
    deadline = time.time() + secs
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        r, _, _ = select.select(fds, [], [], remaining)
        for fd in r:
            try:
                data = os.read(fd, 64)
                name = 'HID' if fd == hid_fd else 'SER'
                hexdump(data, f'{label} {name}')
                got = True
            except OSError:
                pass
    return got

# ── open interfaces ──────────────────────────────────────────────────────────
try:
    hid_fd = os.open(HIDRAW, os.O_RDWR | os.O_NONBLOCK)
except PermissionError:
    sys.exit(f'Cannot open {HIDRAW} — run with sudo')

try:
    ser_fd = os.open(SERIAL, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    # configure 115200 8N1 raw
    attrs = termios.tcgetattr(ser_fd)
    attrs[0] = 0            # iflag: no input processing
    attrs[1] = 0            # oflag: no output processing
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
    attrs[3] = 0            # lflag: raw
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    termios.tcsetattr(ser_fd, termios.TCSANOW, attrs)
    has_serial = True
except Exception as e:
    print(f'Serial: {e}')
    ser_fd = None
    has_serial = False

fds = [hid_fd] + ([ser_fd] if has_serial else [])

# ── phase 1: just listen ─────────────────────────────────────────────────────
print(f'\n=== Phase 1: passive listen {TIMEOUT}s (move pen on tablet) ===')
got = drain(fds, TIMEOUT, 'passive')
if not got:
    print('  (no data — device is silent without init)')

# ── phase 2: try HID output reports ─────────────────────────────────────────
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
    r, _, _ = select.select(fds, [], [], 0.3)
    if r:
        for fd in r:
            data = os.read(fd, 64)
            hexdump(data, 'HID' if fd == hid_fd else 'SER')
    else:
        print('no response')

# ── phase 3: try serial strings ──────────────────────────────────────────────
if has_serial:
    print(f'\n=== Phase 3: serial probe ===')
    serial_cmds = [
        b'\x01\x00',
        b'start\r\n',
        b'\x02\x01\x00',
        b'\x00',
    ]
    for cmd in serial_cmds:
        try:
            os.write(ser_fd, cmd)
        except OSError as e:
            print(f'  write {cmd!r}: {e}')
            continue
        print(f'  sent {cmd.hex()}', end=' → ')
        sys.stdout.flush()
        r, _, _ = select.select(fds, [], [], 0.3)
        if r:
            for fd in r:
                data = os.read(fd, 64)
                hexdump(data, 'HID' if fd == hid_fd else 'SER')
        else:
            print('no response')

# ── phase 4: passive listen again after probes ───────────────────────────────
print(f'\n=== Phase 4: listen {TIMEOUT}s after probes (move pen) ===')
drain(fds, TIMEOUT, 'post-probe')

os.close(hid_fd)
if has_serial:
    os.close(ser_fd)
print('\nDone.')
