#!/usr/bin/env python3
"""
Desktop browser for drawings stored on an ISKN Repaper.

Lists what is on the tablet, previews each drawing, exports it, and deletes
it.  Talking to the device blocks for seconds at a time, so every device
operation runs on a worker thread and reports back through a queue that the
Tk main loop drains; the interface never blocks on serial I/O.

Stdlib only -- tkinter for the interface, and the export helpers shell out to
whichever of inkscape, ImageMagick or GIMP happen to be installed.
"""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import repaper_files
from repaper_uinput import find_serial, open_serial

BLOCK_DEVICE_NAME = 0x14
POLL_MS = 80

# Ink-on-paper, kept deliberately quiet so the drawings carry the colour.
PAPER = '#f7f9f9'
INK = '#14181a'
MUTED = '#616e72'
ACCENT = '#0d6a73'
RULE = '#ccd4d6'


# --------------------------------------------------------------------------
# Device worker
# --------------------------------------------------------------------------

class Device:
    """Serialises device access onto one background thread."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, name, func):
        self.jobs.put((name, func))

    def _run(self):
        while True:
            name, func = self.jobs.get()
            fd = None

            def report(text, fraction):
                """Called from the worker; the UI drains these each tick."""
                self.results.put((name, 'progress', (text, fraction)))

            try:
                serial = find_serial()
                if not serial:
                    raise RuntimeError('tablet not found; plug it in and '
                                       'switch it on')
                fd = open_serial(serial)
                self.results.put((name, 'ok', func(fd, report)))
            except Exception as err:                      # surfaced in the UI
                self.results.put((name, 'error', str(err)))
            finally:
                if fd is not None:
                    try:
                        os.write(fd, repaper_files.packet(
                            repaper_files.BLOCK_SUBSCRIBE, b'\x00\x00'))
                    except OSError:
                        pass
                    os.close(fd)

    def drain(self):
        while True:
            try:
                name, status, payload = self.results.get_nowait()
            except queue.Empty:
                return
            self.on_event(name, status, payload)


def read_device_name(fd):
    repaper_files.quiesce(fd)
    os.write(fd, repaper_files.packet(repaper_files.BLOCK_REQUEST,
                                      [BLOCK_DEVICE_NAME]))
    for block, payload in repaper_files.collect(fd, 2.0, idle=1.0):
        if block == BLOCK_DEVICE_NAME:
            return payload.split(b'\x00')[0].decode('ascii', 'replace')
    return None


def job_refresh(fd, report):
    report('reading the device name', 0.2)
    name = read_device_name(fd)
    report('reading the file table', 0.6)
    status, files = repaper_files.list_files(fd)
    return {'name': name, 'status': status, 'files': files}


def job_download(file_id, expected):
    def run(fd, report):
        def on_chunk(done, total):
            report(f'downloading file {file_id}: chunk {done} of {total}',
                   done / total if total else 0.0)

        blob, problem = repaper_files.download(fd, file_id, expected,
                                               progress=on_chunk)
        if blob is None:
            raise RuntimeError(problem)
        return {'id': file_id, 'blob': blob, 'problem': problem}
    return run


def job_delete(file_id, known_ids):
    def run(fd, report):
        ok, message = repaper_files.remove_file(
            fd, file_id, progress=report, known_ids=known_ids)
        if not ok:
            raise RuntimeError(message)
        return {'id': file_id, 'message': message}
    return run


# --------------------------------------------------------------------------
# Export helpers
# --------------------------------------------------------------------------

def export_png(svg_text, out_path, width=1600):
    """Rasterise via whichever converter is installed."""
    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / 'drawing.svg'
        svg_path.write_text(svg_text)

        if shutil.which('inkscape'):
            cmd = ['inkscape', '--export-type=png',
                   f'--export-filename={out_path}',
                   f'--export-width={width}', str(svg_path)]
        elif shutil.which('convert'):
            cmd = ['convert', '-background', 'none', '-resize', str(width),
                   str(svg_path), str(out_path)]
        else:
            raise RuntimeError('no PNG converter found; install inkscape or '
                               'imagemagick')
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return out_path


def export_xcf(svg_text, out_path, width=1600):
    """Build a layered XCF through GIMP's batch interpreter."""
    if not shutil.which('gimp'):
        raise RuntimeError('gimp is not installed')

    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / 'drawing.svg'
        svg_path.write_text(svg_text)
        script = (
            '(let* ((img (car (file-svg-load RUN-NONINTERACTIVE "%s" "%s" '
            '90.0 %d 0 0))))'
            '  (gimp-image-set-filename img "%s")'
            '  (gimp-file-save RUN-NONINTERACTIVE img '
            '                  (car (gimp-image-get-active-drawable img)) '
            '                  "%s" "%s")'
            '  (gimp-quit 0))'
        ) % (svg_path, svg_path, width, out_path, out_path, out_path)
        subprocess.run(['gimp', '-i', '-b', script],
                       check=True, capture_output=True, timeout=180)
    return out_path


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------

class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=0)
        self.pack(fill='both', expand=True)

        self.device = Device(self.on_device_event)
        self.files = {}
        self.blobs = {}
        self.strokes = {}
        self.busy = 0

        self._build()
        self.after(POLL_MS, self._poll)
        self.refresh()

    # -- layout ----------------------------------------------------------
    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('.', background=PAPER, foreground=INK)
        style.configure('Head.TLabel', font=('TkDefaultFont', 13, 'bold'))
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Treeview', rowheight=24, fieldbackground=PAPER)
        style.configure('Treeview.Heading', font=('TkDefaultFont', 9, 'bold'))

        header = ttk.Frame(self, padding=(14, 12, 14, 10))
        header.pack(fill='x')
        self.title_label = ttk.Label(header, text='Repaper',
                                     style='Head.TLabel')
        self.title_label.pack(side='left')
        self.summary_label = ttk.Label(header, text='connecting...',
                                       style='Muted.TLabel')
        self.summary_label.pack(side='left', padx=(12, 0))
        ttk.Button(header, text='Refresh',
                   command=self.refresh).pack(side='right')

        ttk.Separator(self).pack(fill='x')

        # The status bar claims its space before the expanding pane, or the
        # pane takes the whole window and pushes it off the bottom edge.
        footer = ttk.Frame(self, padding=(14, 7))
        footer.pack(side='bottom', fill='x')
        ttk.Separator(self).pack(side='bottom', fill='x')

        self.status = ttk.Label(footer, text='', style='Muted.TLabel')
        self.status.pack(side='left')
        self.progress = ttk.Progressbar(footer, mode='determinate',
                                        length=190, maximum=1.0)
        # Only shown while work is in flight, so an idle window stays quiet.
        self.progress_visible = False

        panes = ttk.PanedWindow(self, orient='horizontal')
        panes.pack(fill='both', expand=True)

        left = ttk.Frame(panes, padding=(10, 10))
        panes.add(left, weight=0)

        columns = ('size', 'strokes', 'points')
        self.tree = ttk.Treeview(left, columns=columns, show='tree headings',
                                 selectmode='browse', height=14)
        self.tree.heading('#0', text='id')
        self.tree.column('#0', width=48, anchor='w', stretch=False)
        for name, width in (('size', 84), ('strokes', 64), ('points', 68)):
            self.tree.heading(name, text=name)
            self.tree.column(name, width=width, anchor='e', stretch=False)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.on_select)

        right = ttk.Frame(panes, padding=(10, 10))
        panes.add(right, weight=1)

        # Controls before the canvas again: the canvas expands, so anything
        # packed after it gets pushed past the bottom of the pane.
        actions = ttk.Frame(right)
        actions.pack(side='bottom', fill='x')

        self.caption = ttk.Label(right, text='Select a drawing.',
                                 style='Muted.TLabel')
        self.caption.pack(side='bottom', anchor='w', pady=(8, 8))

        self.canvas = tk.Canvas(right, background=PAPER, highlightthickness=1,
                                highlightbackground=RULE, width=420, height=320)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda _e: self.redraw())

        self.buttons = {}
        for label, command in (('Export SVG', self.export_svg),
                               ('Export PNG', self.export_png),
                               ('Export XCF', self.export_xcf)):
            button = ttk.Button(actions, text=label, command=command,
                                state='disabled')
            button.pack(side='left', padx=(0, 6))
            self.buttons[label] = button
        self.delete_button = ttk.Button(actions, text='Delete from device',
                                        command=self.delete_selected,
                                        state='disabled')
        self.delete_button.pack(side='right')


    # -- helpers ---------------------------------------------------------
    def set_status(self, text):
        self.status.configure(text=text)

    def show_progress(self, fraction):
        if not self.progress_visible:
            self.progress.pack(side='right')
            self.progress_visible = True
        self.progress.configure(value=max(0.0, min(1.0, fraction)))

    def hide_progress(self):
        if self.progress_visible:
            self.progress.pack_forget()
            self.progress_visible = False

    def start_job(self, name, func, status):
        self.busy += 1
        self.set_status(status)
        self.show_progress(0.0)
        self.device.submit(name, func)

    def selected_id(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _poll(self):
        self.device.drain()
        self.after(POLL_MS, self._poll)

    # -- device events ---------------------------------------------------
    def on_device_event(self, name, status, payload):
        if status == 'progress':
            text, fraction = payload
            self.set_status(text)
            self.show_progress(fraction)
            return

        self.busy = max(0, self.busy - 1)
        if self.busy == 0:
            self.hide_progress()
        if status == 'error':
            self.set_status(f'{name}: {payload}')
            if name == 'refresh':
                self.summary_label.configure(text='not connected')
            return

        if name == 'refresh':
            self.apply_listing(payload)
        elif name == 'download':
            self.apply_download(payload)
        elif name == 'delete':
            self.set_status(payload['message'])
            self.blobs.pop(payload['id'], None)
            self.strokes.pop(payload['id'], None)
            self.refresh()

    def apply_listing(self, payload):
        if payload['name']:
            self.title_label.configure(text=payload['name'])
        status = payload['status']
        files = sorted(payload['files'], key=lambda f: f['id'])
        self.files = {entry['id']: entry for entry in files}

        total = sum(entry['size'] for entry in files)
        summary = f'{len(files)} drawings   {total:,} bytes'
        if status:
            summary += f'   free {status["free_space"]:,}'
        self.summary_label.configure(text=summary)

        keep = self.selected_id()
        self.tree.delete(*self.tree.get_children())
        for entry in files:
            strokes = self.strokes.get(entry['id'])
            self.tree.insert(
                '', 'end', iid=str(entry['id']), text=f'{entry["id"]:02d}',
                values=(f'{entry["size"]:,}',
                        len(strokes) if strokes else '',
                        f'{sum(len(s) for s in strokes):,}' if strokes else ''))
        if keep is not None and str(keep) in self.tree.get_children():
            self.tree.selection_set(str(keep))
        self.set_status(f'{len(files)} drawings on the device.')

    def apply_download(self, payload):
        file_id = payload['id']
        self.blobs[file_id] = payload['blob']
        try:
            self.strokes[file_id] = repaper_files.parse_drawing(payload['blob'])
        except ValueError as err:
            self.set_status(f'file {file_id}: {err}')
            return

        strokes = self.strokes[file_id]
        points = sum(len(s) for s in strokes)
        if str(file_id) in self.tree.get_children():
            self.tree.set(str(file_id), 'strokes', len(strokes))
            self.tree.set(str(file_id), 'points', f'{points:,}')
        note = payload['problem']
        self.set_status(f'file {file_id}: {len(strokes)} strokes, '
                        f'{points:,} points' + (f'  ({note})' if note else ''))
        if self.selected_id() == file_id:
            self.redraw()
            self.enable_exports(True)

    # -- interaction -----------------------------------------------------
    def refresh(self):
        self.start_job('refresh', job_refresh, 'Reading the file table...')

    def on_select(self, _event=None):
        file_id = self.selected_id()
        if file_id is None:
            return
        self.enable_exports(file_id in self.strokes)
        self.delete_button.configure(state='normal')
        if file_id in self.strokes:
            self.redraw()
            return
        self.canvas.delete('all')
        self.caption.configure(text=f'Loading drawing {file_id}...')
        expected = self.files.get(file_id, {}).get('size')
        self.start_job('download', job_download(file_id, expected),
                       f'Downloading file {file_id}...')

    def enable_exports(self, enabled):
        state = 'normal' if enabled else 'disabled'
        for button in self.buttons.values():
            button.configure(state=state)

    def redraw(self):
        self.canvas.delete('all')
        file_id = self.selected_id()
        strokes = self.strokes.get(file_id)
        if not strokes:
            return

        points = [p for stroke in strokes for p in stroke]
        min_x = min(x for x, _ in points)
        max_x = max(x for x, _ in points)
        min_y = min(y for _, y in points)
        max_y = max(y for _, y in points)
        span_x = max(max_x - min_x, 1)
        span_y = max(max_y - min_y, 1)

        pad = 24
        width = max(self.canvas.winfo_width(), 80)
        height = max(self.canvas.winfo_height(), 80)
        scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)
        off_x = (width - span_x * scale) / 2
        off_y = (height - span_y * scale) / 2

        for stroke in strokes:
            if len(stroke) < 2:
                continue
            flat = []
            for x, y in stroke:
                flat.append((x - min_x) * scale + off_x)
                flat.append((y - min_y) * scale + off_y)
            self.canvas.create_line(*flat, fill=INK, width=2,
                                    capstyle='round', joinstyle='round',
                                    smooth=True)

        entry = self.files.get(file_id, {})
        self.caption.configure(
            text=f'{len(strokes)} strokes   '
                 f'{sum(len(s) for s in strokes):,} points   '
                 f'{span_x / 100:.1f} x {span_y / 100:.1f} mm   '
                 f'{entry.get("date", "")}')

    # -- exports ---------------------------------------------------------
    def _svg_for_selection(self):
        file_id = self.selected_id()
        strokes = self.strokes.get(file_id)
        if not strokes:
            return None, None
        return file_id, repaper_files.strokes_to_svg(strokes)

    def _ask_path(self, file_id, suffix):
        return filedialog.asksaveasfilename(
            defaultextension=suffix,
            initialfile=f'repaper-{file_id:02d}{suffix}',
            filetypes=[(suffix.lstrip('.').upper(), f'*{suffix}')])

    def _export(self, suffix, writer):
        file_id, svg = self._svg_for_selection()
        if svg is None:
            return
        path = self._ask_path(file_id, suffix)
        if not path:
            return
        try:
            writer(svg, path)
        except (OSError, RuntimeError, subprocess.SubprocessError) as err:
            messagebox.showerror('Export failed', str(err))
            return
        self.set_status(f'Saved {path}')

    def export_svg(self):
        self._export('.svg', lambda svg, path: Path(path).write_text(svg))

    def export_png(self):
        self._export('.png', lambda svg, path: export_png(svg, path))

    def export_xcf(self):
        self._export('.xcf', lambda svg, path: export_xcf(svg, path))

    def delete_selected(self):
        file_id = self.selected_id()
        if file_id is None:
            return
        entry = self.files.get(file_id, {})
        if not messagebox.askyesno(
                'Delete from device',
                f'Permanently delete drawing {file_id:02d} '
                f'({entry.get("size", 0):,} bytes) from the tablet?\n\n'
                'This cannot be undone. Export it first if you want a copy.',
                icon='warning', default='no'):
            return
        # The listing on screen is current, so the worker can skip its own
        # pre-check read and go straight to the delete.
        self.start_job('delete', job_delete(file_id, set(self.files)),
                       f'Deleting file {file_id}...')


def main():
    root = tk.Tk()
    root.title('Repaper')
    root.geometry('900x600')
    root.minsize(720, 480)
    root.configure(background=PAPER)
    App(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
