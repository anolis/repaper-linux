#!/usr/bin/env python3
"""
Confine the tablet to one monitor.

On X11 a tablet is an absolute pointer, so by default it maps across the
whole desktop.  Spanning several monitors that makes the pen unusable: a
few centimetres of travel crosses the entire desktop, and the aspect ratio
is badly wrong, so circles come out as ellipses.

This sets the device's Coordinate Transformation Matrix to map the surface
onto one output.  By default it also preserves the aspect ratio, using the
largest correctly-proportioned rectangle that fits, so shapes keep their
proportions instead of being stretched to fill the screen.

The surface aspect is read from the kernel driver's own axis ranges and
resolution, so it follows whatever calibration is in force.
"""

import argparse
import fcntl
import glob
import os
import re
import struct
import subprocess
import time
import sys

DEVICE_NAMES = ('ISKN Repaper Pen', 'ISKN Repaper Virtual Tablet')
CTM_PROPERTY = 'Coordinate Transformation Matrix'

ABS_X, ABS_Y = 0x00, 0x01


def eviocgabs(axis):
    return (2 << 30) | (24 << 16) | (0x45 << 8) | (0x40 + axis)


def run(*command):
    return subprocess.run(command, capture_output=True, text=True,
                          check=False).stdout


def xinput_devices():
    """Map pointer device names to ids."""
    devices = {}
    for line in run('xinput', 'list', '--short').splitlines():
        match = re.search(r'↳\s+(.+?)\s+id=(\d+)\s+\[slave\s+pointer', line)
        if match:
            devices[match.group(1).strip()] = int(match.group(2))
    return devices


def find_device(explicit=None):
    devices = xinput_devices()
    if explicit:
        if explicit in devices:
            return explicit, devices[explicit]
        raise SystemExit(f'no pointer device named {explicit!r}')
    for name, device_id in devices.items():
        if any(name.startswith(prefix) for prefix in DEVICE_NAMES):
            return name, device_id
    raise SystemExit('tablet not found among X pointer devices; is the '
                     'driver loaded?')


def monitors():
    """Return [(name, width, height, x, y)] from xrandr."""
    found = []
    for line in run('xrandr', '--listmonitors').splitlines()[1:]:
        match = re.search(r'(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)', line)
        if match:
            width, height, x, y, name = match.groups()
            found.append((name, int(width), int(height), int(x), int(y)))
    return found


def desktop_size():
    for line in run('xdpyinfo').splitlines():
        match = re.search(r'dimensions:\s+(\d+)x(\d+)', line)
        if match:
            return int(match.group(1)), int(match.group(2))
    raise SystemExit('cannot determine the desktop size')


def surface_aspect():
    """Width/height of the drawing surface, from the driver's own axes."""
    for path in sorted(glob.glob('/sys/class/input/event*/device/name')):
        try:
            name = open(path).read().strip()
        except OSError:
            continue
        if not any(name.startswith(prefix) for prefix in DEVICE_NAMES):
            continue
        node = '/dev/input/' + path.split('/')[4]
        try:
            fd = os.open(node, os.O_RDONLY)
        except OSError:
            continue
        try:
            spans = []
            for axis in (ABS_X, ABS_Y):
                info = fcntl.ioctl(fd, eviocgabs(axis), bytes(24))
                _, low, high, _, _, resolution = struct.unpack('6i', info)
                spans.append((high - low) / (resolution or 1))
        finally:
            os.close(fd)
        if spans[1]:
            return spans[0] / spans[1], spans
    return None, None


def compute_matrix(target, desktop, aspect=None):
    """Matrix mapping the whole surface onto `target` within `desktop`."""
    _, mon_w, mon_h, mon_x, mon_y = target
    desk_w, desk_h = desktop

    width, height = mon_w, mon_h
    if aspect:
        # Fit the largest rectangle of the surface's own proportions, so a
        # square drawn on the tablet stays square on screen.
        if mon_w / mon_h > aspect:
            width = mon_h * aspect
        else:
            height = mon_w / aspect

    offset_x = mon_x + (mon_w - width) / 2
    offset_y = mon_y + (mon_h - height) / 2

    return [width / desk_w, 0.0, offset_x / desk_w,
            0.0, height / desk_h, offset_y / desk_h,
            0.0, 0.0, 1.0]


def apply_matrix(device_id, matrix):
    subprocess.run(['xinput', 'set-prop', str(device_id), '--type=float',
                    CTM_PROPERTY] + [f'{value:.6f}' for value in matrix],
                   check=True)


IDENTITY = [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0]


def main():
    parser = argparse.ArgumentParser(
        description='Map the Repaper onto a single monitor.')
    parser.add_argument('output', nargs='?',
                        help='monitor to map onto, e.g. DP-5')
    parser.add_argument('--device', help='X pointer device name to configure')
    parser.add_argument('--list', action='store_true',
                        help='show monitors and the current mapping')
    parser.add_argument('--stretch', action='store_true',
                        help='fill the monitor, distorting the aspect ratio')
    parser.add_argument('--reset', action='store_true',
                        help='map across the whole desktop again')
    parser.add_argument('--watch', action='store_true',
                        help='keep the mapping applied as the device reappears')
    args = parser.parse_args()

    if not os.environ.get('DISPLAY'):
        sys.exit('DISPLAY is not set; this only applies to an X session.')

    name, device_id = find_device(args.device)
    desktop = desktop_size()
    aspect, spans = surface_aspect()

    if args.list or not (args.output or args.reset):
        print(f'device : {name} (id {device_id})')
        print(f'desktop: {desktop[0]}x{desktop[1]}')
        if spans:
            print(f'surface: {spans[0]:.0f} x {spans[1]:.0f} mm  '
                  f'aspect {aspect:.3f}')
        print('\nmonitors:')
        for monitor in monitors():
            mon_name, width, height, x, y = monitor
            print(f'  {mon_name:10s} {width}x{height} at +{x}+{y}  '
                  f'aspect {width / height:.3f}')
        print(f'\nMap onto one with:  {sys.argv[0]} <output>')
        return 0

    if args.reset:
        apply_matrix(device_id, IDENTITY)
        print(f'{name}: mapped across the whole desktop again')
        return 0

    targets = {monitor[0]: monitor for monitor in monitors()}
    if args.output not in targets:
        sys.exit(f'no monitor named {args.output!r}; '
                 f'have {", ".join(sorted(targets))}')

    matrix = compute_matrix(targets[args.output], desktop,
                            None if args.stretch else aspect)
    apply_matrix(device_id, matrix)

    _, mon_w, mon_h, _, _ = targets[args.output]
    used_w = matrix[0] * desktop[0]
    used_h = matrix[4] * desktop[1]
    print(f'{name} -> {args.output}')
    print(f'  drawing area: {used_w:.0f}x{used_h:.0f} px of {mon_w}x{mon_h}')
    if not args.stretch and aspect:
        print(f'  aspect preserved at {aspect:.3f}')

    if not args.watch:
        print('\nThe mapping belongs to this X device, and the device is '
              'recreated whenever the driver reloads or the pen returns to '
              'proximity after being gone. Re-run then, or use --watch.')
        return 0

    return watch_loop(args, matrix)


def watch_loop(args, matrix):
    """Reapply the mapping whenever X recreates the tool device.

    libinput creates the pen's tool device lazily, the first time a stylus
    is detected, and destroys it again when the driver reloads.  Each new
    device starts with an identity matrix, so a mapping applied once
    silently lapses and the cursor goes back to roaming the whole desktop.
    """
    print(f'\nWatching; the mapping is reapplied when the device reappears. '
          f'Ctrl-C to stop.')
    known = None
    try:
        while True:
            try:
                _, device_id = find_device(args.device)
            except SystemExit:
                device_id = None
            if device_id is not None and device_id != known:
                apply_matrix(device_id, matrix)
                stamp = time.strftime('%H:%M:%S')
                print(f'  {stamp}  reapplied to device id {device_id}',
                      flush=True)
                known = device_id
            elif device_id is None and known is not None:
                print(f'  {time.strftime("%H:%M:%S")}  device gone; waiting',
                      flush=True)
                known = None
            time.sleep(2.0)
    except KeyboardInterrupt:
        print('\nstopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
