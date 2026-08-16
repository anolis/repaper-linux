#!/usr/bin/env python3
"""
Canvas-only acquisition tester.

Reads the pen directly from its evdev node instead of going through X, so
what you see here is exactly what the driver produces, with nothing in
between to blame.  Use it to answer three questions:

  * is the whole surface reachable, and does it map without distortion
  * do contact, proximity and tilt behave
  * can the tablet drive a canvas without moving the desktop cursor

The last one is what "canvas only" means in practice.  On X11 a tablet is
an absolute pointer, so it drives the cursor by design.  Detaching it from
the core pointer stops that; this window keeps receiving pen data either
way, because it reads the device itself.

Needs read access to /dev/input/event*, which membership of the "input"
group provides.
"""

import argparse
import fcntl
import glob
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

EVENT_STRUCT = struct.Struct('llHHi')

EV_KEY, EV_ABS, EV_SYN = 0x01, 0x03, 0x00
ABS_X, ABS_Y = 0x00, 0x01
ABS_PRESSURE, ABS_DISTANCE = 0x18, 0x19
ABS_TILT_X, ABS_TILT_Y = 0x1a, 0x1b
BTN_TOOL_PEN, BTN_TOUCH = 0x140, 0x14a

DEVICE_NAMES = ('ISKN Repaper Pen', 'ISKN Repaper Virtual Tablet')
CORE_POINTER = 2

PAPER = '#f7f9f9'
INK = '#14181a'
MUTED = '#616e72'
ACCENT = '#0d6a73'
RULE = '#ccd4d6'
HOVER = '#9fb8bb'


def eviocgabs(axis):
    return (2 << 30) | (24 << 16) | (0x45 << 8) | (0x40 + axis)


def find_pen_node(explicit=None):
    if explicit:
        return explicit, explicit
    for path in sorted(glob.glob('/sys/class/input/event*/device/name')):
        try:
            name = open(path).read().strip()
        except OSError:
            continue
        if any(name.startswith(prefix) for prefix in DEVICE_NAMES):
            return '/dev/input/' + path.split('/')[4], name
    return None, None


def axis_info(fd, axis):
    info = fcntl.ioctl(fd, eviocgabs(axis), bytes(24))
    _, low, high, fuzz, flat, resolution = struct.unpack('6i', info)
    return {'min': low, 'max': high, 'res': resolution}


# --------------------------------------------------------------------------
# X pointer attachment
# --------------------------------------------------------------------------

def x_device_id():
    if not os.environ.get('DISPLAY'):
        return None
    listing = subprocess.run(['xinput', 'list', '--short'],
                             capture_output=True, text=True).stdout
    for line in listing.splitlines():
        match = re.search(r'↳\s+(.+?)\s+id=(\d+)\s+\[(slave|floating)', line)
        if match and any(match.group(1).strip().startswith(prefix)
                         for prefix in DEVICE_NAMES):
            return int(match.group(2))
    return None


def is_floating(device_id):
    listing = subprocess.run(['xinput', 'list', '--short'],
                             capture_output=True, text=True).stdout
    for line in listing.splitlines():
        if f'id={device_id}' in line:
            return 'floating' in line
    return False


def set_floating(device_id, floating):
    command = (['xinput', 'float', str(device_id)] if floating else
               ['xinput', 'reattach', str(device_id), str(CORE_POINTER)])
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode == 0, (result.stderr or '').strip()


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------

class PenReader(threading.Thread):
    """Turns evdev packets into complete samples, one per SYN_REPORT."""

    def __init__(self, node, out):
        super().__init__(daemon=True)
        self.node = node
        self.out = out
        self.running = True
        self.state = {'x': None, 'y': None, 'pressure': 0, 'distance': 0,
                      'tilt_x': 0, 'tilt_y': 0, 'touch': False,
                      'proximity': False}

    def run(self):
        fd = os.open(self.node, os.O_RDONLY)
        try:
            while self.running:
                data = os.read(fd, EVENT_STRUCT.size * 64)
                for offset in range(0, len(data) - EVENT_STRUCT.size + 1,
                                    EVENT_STRUCT.size):
                    _, _, etype, code, value = EVENT_STRUCT.unpack(
                        data[offset:offset + EVENT_STRUCT.size])
                    self._apply(etype, code, value)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _apply(self, etype, code, value):
        if etype == EV_ABS:
            field = {ABS_X: 'x', ABS_Y: 'y', ABS_PRESSURE: 'pressure',
                     ABS_DISTANCE: 'distance', ABS_TILT_X: 'tilt_x',
                     ABS_TILT_Y: 'tilt_y'}.get(code)
            if field:
                self.state[field] = value
        elif etype == EV_KEY:
            if code == BTN_TOUCH:
                self.state['touch'] = bool(value)
            elif code == BTN_TOOL_PEN:
                self.state['proximity'] = bool(value)
        elif etype == EV_SYN:
            # One sample per report, so partial updates never reach the UI.
            self.out.put(dict(self.state))


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------

class Tester(ttk.Frame):
    def __init__(self, master, node, name):
        super().__init__(master, padding=0)
        self.pack(fill='both', expand=True)

        self.samples = queue.Queue()
        self.node = node
        self.name = name
        self.last_point = None
        self.count = 0
        self.rate_mark = time.monotonic()
        self.rate = 0.0
        self.seen_pressure = set()
        self.device_id = x_device_id()

        fd = os.open(node, os.O_RDONLY)
        try:
            self.ax = axis_info(fd, ABS_X)
            self.ay = axis_info(fd, ABS_Y)
        finally:
            os.close(fd)

        self._build()
        PenReader(node, self.samples).start()
        self.after(16, self._drain)

    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('.', background=PAPER, foreground=INK)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Head.TLabel', font=('TkDefaultFont', 12, 'bold'))

        header = ttk.Frame(self, padding=(14, 10))
        header.pack(fill='x')
        ttk.Label(header, text=self.name, style='Head.TLabel').pack(side='left')
        ttk.Label(header, text=f'  {self.node}',
                  style='Muted.TLabel').pack(side='left')

        self.detach = tk.BooleanVar(
            value=bool(self.device_id) and is_floating(self.device_id))
        self.detach_box = ttk.Checkbutton(
            header, text='Detach from desktop cursor',
            variable=self.detach, command=self.toggle_detach)
        self.detach_box.pack(side='right')
        if not self.device_id:
            self.detach_box.state(['disabled'])
        ttk.Button(header, text='Clear',
                   command=self.clear).pack(side='right', padx=(0, 10))

        ttk.Separator(self).pack(fill='x')

        footer = ttk.Frame(self, padding=(14, 8))
        footer.pack(side='bottom', fill='x')
        ttk.Separator(self).pack(side='bottom', fill='x')
        self.readout = ttk.Label(footer, text='waiting for the pen...',
                                 style='Muted.TLabel',
                                 font=('TkFixedFont', 9))
        self.readout.pack(side='left')
        self.verdict = ttk.Label(footer, text='', style='Muted.TLabel')
        self.verdict.pack(side='right')

        self.canvas = tk.Canvas(self, background=PAPER, highlightthickness=1,
                                highlightbackground=RULE, width=760, height=520)
        self.canvas.pack(fill='both', expand=True, padx=12, pady=12)
        self.canvas.bind('<Configure>', lambda _e: self.draw_frame())

    # -- geometry --------------------------------------------------------
    def surface_box(self):
        """Rectangle of the full tablet surface, aspect-correct, centred."""
        width = max(self.canvas.winfo_width(), 40)
        height = max(self.canvas.winfo_height(), 40)
        span_x = max(self.ax['max'] - self.ax['min'], 1)
        span_y = max(self.ay['max'] - self.ay['min'], 1)
        aspect = span_x / span_y

        pad = 16
        box_w, box_h = width - 2 * pad, height - 2 * pad
        if box_w / box_h > aspect:
            box_w = box_h * aspect
        else:
            box_h = box_w / aspect
        return ((width - box_w) / 2, (height - box_h) / 2, box_w, box_h)

    def to_canvas(self, x, y):
        left, top, box_w, box_h = self.surface_box()
        fx = (x - self.ax['min']) / max(self.ax['max'] - self.ax['min'], 1)
        fy = (y - self.ay['min']) / max(self.ay['max'] - self.ay['min'], 1)
        return left + fx * box_w, top + fy * box_h

    def draw_frame(self):
        self.canvas.delete('frame')
        left, top, box_w, box_h = self.surface_box()
        self.canvas.create_rectangle(left, top, left + box_w, top + box_h,
                                     outline=RULE, dash=(4, 3), tags='frame')
        self.canvas.create_text(left + 6, top + 6, anchor='nw',
                                text='tablet surface', fill=MUTED,
                                font=('TkDefaultFont', 8), tags='frame')
        self.canvas.tag_lower('frame')

    def clear(self):
        self.canvas.delete('ink')
        self.last_point = None
        self.count = 0
        self.seen_pressure.clear()

    # -- events ----------------------------------------------------------
    def toggle_detach(self):
        wanted = self.detach.get()
        ok, error = set_floating(self.device_id, wanted)
        if not ok:
            self.detach.set(not wanted)
            self.verdict.configure(text=f'xinput: {error}')
            return
        self.verdict.configure(
            text='cursor detached; canvas still receiving' if wanted
            else 'cursor reattached')

    def _drain(self):
        sample = None
        while True:
            try:
                sample = self.samples.get_nowait()
            except queue.Empty:
                break
            self.count += 1
            self.plot(sample)
        if sample:
            self.show(sample)
        now = time.monotonic()
        if now - self.rate_mark >= 1.0:
            self.rate = self.count / (now - self.rate_mark)
            self.count = 0
            self.rate_mark = now
        self.after(16, self._drain)

    def plot(self, sample):
        if sample['x'] is None or sample['y'] is None:
            return
        if not sample['proximity']:
            self.last_point = None
            return

        point = self.to_canvas(sample['x'], sample['y'])
        if sample['touch']:
            if self.last_point:
                self.canvas.create_line(*self.last_point, *point, fill=INK,
                                        width=2, capstyle='round', tags='ink')
            self.last_point = point
        else:
            # Hover leaves a faint trail, so proximity is visible too.
            self.canvas.create_oval(point[0] - 1, point[1] - 1,
                                    point[0] + 1, point[1] + 1,
                                    outline=HOVER, tags='ink')
            self.last_point = None
        self.seen_pressure.add(sample['pressure'])

    def show(self, sample):
        self.readout.configure(
            text=f'x={sample["x"]:>7}  y={sample["y"]:>7}  '
                 f'press={sample["pressure"]:>5}  dist={sample["distance"]:>6}  '
                 f'tilt=({sample["tilt_x"]:>4},{sample["tilt_y"]:>4})  '
                 f'{"TOUCH" if sample["touch"] else "hover" if sample["proximity"] else "  -  "}'
                 f'  {self.rate:5.1f} Hz')
        levels = len(self.seen_pressure)
        if levels > 2:
            self.verdict.configure(text=f'{levels} pressure levels seen')
        elif levels:
            self.verdict.configure(
                text=f'pressure is binary ({levels} level'
                     f'{"s" if levels > 1 else ""})')


def main():
    parser = argparse.ArgumentParser(
        description='Canvas-only acquisition tester for the Repaper.')
    parser.add_argument('--event', help='evdev node, e.g. /dev/input/event21')
    args = parser.parse_args()

    node, name = find_pen_node(args.event)
    if not node:
        sys.exit('No Repaper pen device found. Is hid-iskn loaded, or the '
                 'uinput bridge running?')
    try:
        os.close(os.open(node, os.O_RDONLY))
    except PermissionError:
        sys.exit(f'Cannot read {node}. Add yourself to the "input" group.')

    root = tk.Tk()
    root.title('Repaper acquisition')
    root.geometry('820x660')
    root.configure(background=PAPER)
    Tester(root, node, name or 'Repaper pen')
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
