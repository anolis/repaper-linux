#!/usr/bin/env python3
"""
Bridge the ISKN Repaper pen stream into a Linux uinput tablet.

Subscribes to the pen3d auto-block (0x05), which carries everything pen2d
does plus a height field, and exposes a virtual absolute tablet that GIMP,
Krita and libinput can consume.

Protocol notes established by measurement:

  * subscribe (block 0x33) takes a 16-bit BITMASK, not a stream id.
    Bit N enables the auto-block whose type is 0x02 + N, so pen3d is
    bit 3 == 0x0008.  Passing small integers as ids silently selects
    whichever bits those integers happen to set.
  * pen3d payload is x, y, z, seq, rot_x, rot_y, state.  Field 4 is a
    frame counter that advances by 2 every frame regardless of the pen,
    not a height as previously assumed.
  * z saturates at +300 on contact and is negative while hovering, so it
    is a proximity signal.  The tablet reports no real pen pressure.
"""

import argparse
import fcntl
import math
import os
import select
import struct
import sys
import termios
import time
from pathlib import Path

from decode_stream import (
    FRAME_SIZES,
    SIGNATURE,
    auto_block_mask,
    crc16_ccitt,
)

USB_VENDOR = '2c87'
USB_PRODUCT = '0001'
SERIAL_SPEED = getattr(termios, 'B4000000', termios.B115200)

PEN2D_BLOCK = 0x04
PEN3D_BLOCK = 0x05

REQUEST_BLOCK = 0x34
SUBSCRIBE_BLOCK = 0x33

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0x00

BTN_TOOL_PEN = 0x140
BTN_TOUCH = 0x14a

ABS_X = 0x00
ABS_Y = 0x01
ABS_PRESSURE = 0x18
ABS_DISTANCE = 0x19
ABS_TILT_X = 0x1a
ABS_TILT_Y = 0x1b

INPUT_PROP_POINTER = 0x00
INPUT_PROP_DIRECT = 0x01

BUS_USB = 0x03

# Values taken from <linux/uinput.h> on this kernel.
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503
UI_ABS_SETUP = 0x401C5504
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E

# Bounds from a full-surface sweep of a real unit.  At 100 units/mm these
# describe a 158 x 215 mm surface, which matches A5 paper (148 x 210 mm)
# with the small excess the magnet tracks past the sheet edge.  Override
# with --config or --calibrate rather than editing these.
DEFAULT_X_MIN = -7858
DEFAULT_X_MAX = 7916
DEFAULT_Y_MIN = -10458
DEFAULT_Y_MAX = 10995

# z is a height, not a force: it pins to +300 in contact and runs negative
# while hovering.
DEFAULT_Z_MIN = -5200
DEFAULT_Z_MAX = 300

DEFAULT_PRESSURE = 1024

# Raw units per millimetre.  The vendor API scales raw values by 0.01 and
# treats the result as millimetres, so 100 raw units is 1 mm.
RESOLUTION = 100
DEFAULT_RESOLUTION = RESOLUTION

# rot_x and rot_y are the x/y components of the pen's unit orientation
# vector, scaled by 10000.  Real samples therefore satisfy
# rot_x^2 + rot_y^2 <= 10000^2; with no pen over the surface the tablet
# emits vectors well outside the unit circle, which is what makes this a
# reliable proximity test as well as the basis for tilt.
ROT_UNIT = 10000
# Held pens reach a magnitude near 11200 at steep tilt while the idle noise
# sits near 12800, so the cutoff has to sit between the two.
ROT_TOLERANCE = 1.2
TILT_LIMIT = 90

# Drop the tool out of proximity after this long without a usable sample.
DEFAULT_PROXIMITY_MS = 120

CONFIG_PATH = Path.home() / '.config' / 'repaper' / 'calibration.conf'


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


def iskn_packet(block_type, payload):
    payload = bytes(payload)
    crc = crc16_ccitt(payload)
    return SIGNATURE + bytes([block_type]) + payload + crc.to_bytes(2, 'little')


def subscribe(fd, *block_types):
    """Enable exactly the given auto-blocks and silence everything else."""
    mask = auto_block_mask(*block_types) if block_types else 0
    os.write(fd, iskn_packet(SUBSCRIBE_BLOCK, mask.to_bytes(2, 'little')))
    return mask


def iter_frames_from_buffer(buffer):
    while True:
        start = buffer.find(SIGNATURE)
        if start < 0:
            # Keep the last two bytes: a signature may straddle the boundary.
            del buffer[:max(len(buffer) - 2, 0)]
            return
        if start:
            del buffer[:start]
        if len(buffer) < 4:
            return

        frame_len = FRAME_SIZES.get(buffer[3])
        if frame_len is None:
            del buffer[:3]
            continue
        if len(buffer) < frame_len:
            return

        frame = bytes(buffer[:frame_len])
        del buffer[:frame_len]
        yield frame


def parse_pen(frame):
    """Decode a pen2d or pen3d frame into a common sample dict."""
    block = frame[3]
    payload = frame[4:-2]
    expected_crc = int.from_bytes(frame[-2:], 'little')
    if crc16_ccitt(payload) != expected_crc:
        return None

    if block == PEN2D_BLOCK and len(payload) == 9:
        x, y, rot_x, rot_y, state = struct.unpack('<hhhhB', payload)
        z = None
        seq = None
    elif block == PEN3D_BLOCK and len(payload) == 13:
        x, y, z, seq, rot_x, rot_y, state = struct.unpack('<hhhHhhB', payload)
    else:
        return None

    return {
        'x': x,
        'y': y,
        'z': z,
        'seq': seq,
        'rot_x': rot_x,
        'rot_y': rot_y,
        'touch': state != 0,
    }


def clamp(value, low, high):
    return max(low, min(high, value))


def pen_present(sample):
    """True if the orientation vector is physically possible.

    The tablet streams continuously whether or not a pen is over it.  With
    no magnet to track it emits vectors far outside the unit circle, so a
    magnitude check rejects that noise without needing calibration.
    """
    limit = (ROT_UNIT * ROT_TOLERANCE) ** 2
    return sample['rot_x'] ** 2 + sample['rot_y'] ** 2 <= limit


def write_event(fd, event_type, code, value):
    sec, frac = divmod(time.time_ns(), 1_000_000_000)
    usec = frac // 1000
    os.write(fd, struct.pack('llHHi', sec, usec, event_type, code, value))


def sync(fd):
    write_event(fd, EV_SYN, SYN_REPORT, 0)


def axis_bounds(args):
    orientation = normalize_orientation(args.orientation)
    if orientation == 'portrait':
        return args.x_min, args.x_max, args.y_min, args.y_max
    return args.y_min, args.y_max, args.x_min, args.x_max


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


def tilt_degrees(value):
    """Convert one component of the unit orientation vector to degrees."""
    ratio = clamp(value / ROT_UNIT, -1.0, 1.0)
    return int(clamp(round(math.degrees(math.asin(ratio))),
                     -TILT_LIMIT, TILT_LIMIT))


def create_uinput(args):
    fd = os.open(args.uinput, os.O_WRONLY | os.O_NONBLOCK)

    for event_type in (EV_KEY, EV_ABS):
        fcntl.ioctl(fd, UI_SET_EVBIT, event_type)
    for key in (BTN_TOOL_PEN, BTN_TOUCH):
        fcntl.ioctl(fd, UI_SET_KEYBIT, key)

    # An external tablet is a pointer device; INPUT_PROP_DIRECT is for
    # display-integrated tablets and misleads libinput about screen mapping.
    prop = INPUT_PROP_DIRECT if args.direct else INPUT_PROP_POINTER
    fcntl.ioctl(fd, UI_SET_PROPBIT, prop)

    x_min, x_max, y_min, y_max = axis_bounds(args)
    axes = {
        ABS_X: (x_min, x_max, args.resolution),
        ABS_Y: (y_min, y_max, args.resolution),
        ABS_PRESSURE: (0, args.pressure, 0),
        ABS_DISTANCE: (args.z_min, args.z_max, 0),
        ABS_TILT_X: (-TILT_LIMIT, TILT_LIMIT, 0),
        ABS_TILT_Y: (-TILT_LIMIT, TILT_LIMIT, 0),
    }
    for axis in axes:
        fcntl.ioctl(fd, UI_SET_ABSBIT, axis)

    name = args.name.encode('utf-8')[:79]
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack(
        '<4H80sI', BUS_USB, int(USB_VENDOR, 16), int(USB_PRODUCT, 16), 1,
        name, 0))

    # UI_ABS_SETUP is what carries resolution, so applications can map raw
    # units to millimetres instead of guessing.
    for axis, (low, high, resolution) in axes.items():
        fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack(
            '<Hxx6i', axis, 0, low, high, args.fuzz, 0, resolution))

    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit_pen(fd, sample, args):
    x, y = transform_sample(sample, args)
    touch = sample['touch']

    write_event(fd, EV_KEY, BTN_TOOL_PEN, 1)
    write_event(fd, EV_KEY, BTN_TOUCH, int(touch))
    write_event(fd, EV_ABS, ABS_X, x)
    write_event(fd, EV_ABS, ABS_Y, y)
    write_event(fd, EV_ABS, ABS_PRESSURE, args.pressure if touch else 0)
    if sample['z'] is not None:
        write_event(fd, EV_ABS, ABS_DISTANCE,
                    clamp(sample['z'], args.z_min, args.z_max))
    write_event(fd, EV_ABS, ABS_TILT_X, tilt_degrees(sample['rot_x']))
    write_event(fd, EV_ABS, ABS_TILT_Y, tilt_degrees(sample['rot_y']))
    sync(fd)


def emit_out_of_proximity(fd):
    write_event(fd, EV_KEY, BTN_TOUCH, 0)
    write_event(fd, EV_KEY, BTN_TOOL_PEN, 0)
    write_event(fd, EV_ABS, ABS_PRESSURE, 0)
    sync(fd)


def load_config(path, args):
    """Apply calibration written by --calibrate, if present."""
    try:
        text = path.read_text()
    except OSError:
        return False

    mapping = {
        'REPAPER_X_MIN': 'x_min', 'REPAPER_X_MAX': 'x_max',
        'REPAPER_Y_MIN': 'y_min', 'REPAPER_Y_MAX': 'y_max',
        'REPAPER_Z_MIN': 'z_min', 'REPAPER_Z_MAX': 'z_max',
    }
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        attr = mapping.get(key.strip())
        if attr:
            try:
                setattr(args, attr, int(value.strip()))
            except ValueError:
                pass
    return True


def run_calibration(ser_fd, args):
    """Collect bounds while the user sweeps the surface, then save them."""
    subscribe(ser_fd, PEN3D_BLOCK)
    print(f'Sweep the pen over the whole surface for {args.calibrate}s.')
    print('Reach all four corners; press down for part of it.')

    buffer = bytearray()
    samples = []
    deadline = time.time() + args.calibrate
    last_tick = -1

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        tick = int(remaining)
        if tick != last_tick:
            print(f'  {tick + 1:3d}s  samples={len(samples)}', end='\r',
                  flush=True)
            last_tick = tick

        readable, _, _ = select.select([ser_fd], [], [], min(remaining, 0.2))
        if not readable:
            continue
        try:
            chunk = os.read(ser_fd, 4096)
        except (BlockingIOError, OSError):
            continue
        if not chunk:
            continue
        buffer.extend(chunk)
        for frame in iter_frames_from_buffer(buffer):
            sample = parse_pen(frame)
            if sample is not None:
                samples.append(sample)

    print()
    touching = [s for s in samples if s['touch']]
    if len(touching) < 50:
        print(f'Only {len(touching)} contact samples out of {len(samples)}.')
        print('Calibration needs the pen pressed on the surface; not saving.')
        return 1

    bounds = {
        'REPAPER_X_MIN': min(s['x'] for s in touching),
        'REPAPER_X_MAX': max(s['x'] for s in touching),
        'REPAPER_Y_MIN': min(s['y'] for s in touching),
        'REPAPER_Y_MAX': max(s['y'] for s in touching),
        'REPAPER_Z_MIN': min(s['z'] for s in samples if s['z'] is not None),
        'REPAPER_Z_MAX': max(s['z'] for s in samples if s['z'] is not None),
    }

    args.config.parent.mkdir(parents=True, exist_ok=True)
    lines = ['# ISKN Repaper calibration',
             f'# {len(touching)} contact samples of {len(samples)} total']
    lines += [f'{key}={value}' for key, value in bounds.items()]
    args.config.write_text('\n'.join(lines) + '\n')

    print(f'Saved {args.config}')
    for key, value in bounds.items():
        print(f'  {key}={value}')

    width_mm = (bounds['REPAPER_X_MAX'] - bounds['REPAPER_X_MIN']) / RESOLUTION
    height_mm = (bounds['REPAPER_Y_MAX'] - bounds['REPAPER_Y_MIN']) / RESOLUTION
    print(f'\nSurface: {width_mm:.0f} x {height_mm:.0f} mm '
          f'at {RESOLUTION} units/mm')

    # The kernel driver cannot read this file, so hand over the equivalent
    # module parameters.
    params = (f'x_min={bounds["REPAPER_X_MIN"]} x_max={bounds["REPAPER_X_MAX"]} '
              f'y_min={bounds["REPAPER_Y_MIN"]} y_max={bounds["REPAPER_Y_MAX"]}')
    modprobe = args.config.parent / 'modprobe-hid-iskn.conf'
    modprobe.write_text(f'# Generated by repaper_uinput.py --calibrate\n'
                        f'options hid-iskn {params}\n')
    print(f'\nFor the kernel driver, wrote {modprobe}')
    print(f'  sudo cp {modprobe} /etc/modprobe.d/hid-iskn.conf')
    print(f'  or load directly:  sudo insmod ./hid-iskn.ko {params}')
    return 0


def stream(ser_fd, ui_fd, args):
    subscribe(ser_fd, PEN3D_BLOCK if args.block == 'pen3d' else PEN2D_BLOCK)
    buffer = bytearray()
    in_proximity = False
    last_sample = 0.0
    proximity_timeout = args.proximity_ms / 1000.0

    while True:
        readable, _, _ = select.select([ser_fd], [], [], 0.05)

        if readable:
            try:
                chunk = os.read(ser_fd, 4096)
            except OSError:
                return 'disconnected'
            # A zero-length read on a ready fd means the far end is gone;
            # returning here avoids spinning the CPU on a dead device.
            if not chunk:
                return 'disconnected'
            buffer.extend(chunk)

            for frame in iter_frames_from_buffer(buffer):
                sample = parse_pen(frame)
                if sample is None or not pen_present(sample):
                    continue
                emit_pen(ui_fd, sample, args)
                in_proximity = True
                last_sample = time.monotonic()
                if args.verbose:
                    out_x, out_y = transform_sample(sample, args)
                    print(f'raw=({sample["x"]:6d},{sample["y"]:6d}) '
                          f'out=({out_x:6d},{out_y:6d}) '
                          f'z={sample["z"]} '
                          f'tilt=({tilt_degrees(sample["rot_x"]):4d},'
                          f'{tilt_degrees(sample["rot_y"]):4d}) '
                          f'touch={sample["touch"]}', flush=True)

        if in_proximity and time.monotonic() - last_sample > proximity_timeout:
            # Without this the pen never leaves proximity and applications
            # keep a stale cursor forever once it is lifted away.
            emit_out_of_proximity(ui_fd)
            in_proximity = False


def wait_for_serial(timeout=None):
    deadline = None if timeout is None else time.time() + timeout
    announced = False
    while deadline is None or time.time() < deadline:
        serial = find_serial()
        if serial:
            return serial
        if not announced:
            print('Waiting for the tablet (plug in and power on)...')
            announced = True
        time.sleep(1.0)
    return None


def build_parser():
    parser = argparse.ArgumentParser(
        description='Expose the ISKN Repaper pen stream as a uinput tablet.',
    )
    parser.add_argument('--serial', help='serial node, e.g. /dev/ttyACM0')
    parser.add_argument('--uinput', default='/dev/uinput',
                        help='uinput node to create the virtual tablet')
    parser.add_argument('--name', default='ISKN Repaper Virtual Tablet',
                        help='virtual input device name')
    parser.add_argument('--block', default='pen3d', choices=('pen2d', 'pen3d'),
                        help='auto-block to subscribe; pen3d adds height')
    parser.add_argument('--x-min', type=int, default=DEFAULT_X_MIN)
    parser.add_argument('--x-max', type=int, default=DEFAULT_X_MAX)
    parser.add_argument('--y-min', type=int, default=DEFAULT_Y_MIN)
    parser.add_argument('--y-max', type=int, default=DEFAULT_Y_MAX)
    parser.add_argument('--z-min', type=int, default=DEFAULT_Z_MIN)
    parser.add_argument('--z-max', type=int, default=DEFAULT_Z_MAX)
    parser.add_argument('--orientation', default='portrait',
                        choices=('portrait', 'landscape', 'landscape-cw',
                                 'landscape-ccw'),
                        help='tablet orientation; landscape aliases landscape-cw')
    parser.add_argument('--pressure', type=int, default=DEFAULT_PRESSURE,
                        help='value reported while in contact (no real pressure)')
    parser.add_argument('--resolution', type=int, default=DEFAULT_RESOLUTION,
                        help='raw units per millimetre reported to applications')
    parser.add_argument('--fuzz', type=int, default=0,
                        help='per-axis fuzz; raise to damp jitter')
    parser.add_argument('--proximity-ms', type=int, default=DEFAULT_PROXIMITY_MS,
                        help='drop the tool after this long without samples')
    parser.add_argument('--direct', action='store_true',
                        help='set INPUT_PROP_DIRECT (display-integrated tablets)')
    parser.add_argument('--config', type=Path, default=CONFIG_PATH,
                        help='calibration file to read and write')
    parser.add_argument('--calibrate', type=int, metavar='SECONDS', nargs='?',
                        const=20,
                        help='record calibration bounds and exit')
    parser.add_argument('--no-reconnect', action='store_true',
                        help='exit on unplug instead of waiting for the tablet')
    parser.add_argument('--verbose', action='store_true',
                        help='print decoded pen samples')
    return parser


def main():
    args = build_parser().parse_args()

    if load_config(args.config, args) and not args.calibrate:
        print(f'Calibration: {args.config}')

    serial = args.serial or wait_for_serial()
    if not serial:
        sys.exit('Cannot find Repaper serial node.')

    if args.calibrate:
        ser_fd = open_serial(serial)
        try:
            return run_calibration(ser_fd, args)
        finally:
            subscribe(ser_fd)
            os.close(ser_fd)

    print(f'Serial: {serial}')
    print(f'Uinput: {args.name}')
    print(f'Block:  {args.block}')
    print(f'Orientation: {normalize_orientation(args.orientation)}')
    print('Listening; press Ctrl-C to stop.')

    while True:
        ser_fd = None
        ui_fd = None
        try:
            ser_fd = open_serial(serial)
            ui_fd = create_uinput(args)
            reason = stream(ser_fd, ui_fd, args)
        except KeyboardInterrupt:
            return 0
        except OSError as err:
            reason = f'error: {err}'
        finally:
            if ui_fd is not None:
                try:
                    emit_out_of_proximity(ui_fd)
                    fcntl.ioctl(ui_fd, UI_DEV_DESTROY)
                except OSError:
                    pass
                os.close(ui_fd)
            if ser_fd is not None:
                try:
                    subscribe(ser_fd)
                except OSError:
                    pass
                os.close(ser_fd)

        print(f'Tablet {reason}.')
        if args.no_reconnect:
            return 1

        serial = args.serial or wait_for_serial()
        if not serial:
            return 1
        print(f'Reconnected: {serial}')


if __name__ == '__main__':
    sys.exit(main())
