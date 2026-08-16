#!/usr/bin/env python3
"""
List and download drawings stored on an ISKN Repaper.

The tablet keeps recorded sessions on internal storage. It does not expose
them as a USB disk -- it has one configuration with only HID and CDC
interfaces, and no mass-storage class -- so they come off over the vendor
protocol instead.

Wire format, from BlockDiskOperation in libISKN_API.so.1.0.0:

    b3 a5 e1 35 <req:u8> <code:u64le> <file_id:u16le> <arg:u32le> <crc16le>

Requests 2 and 3 are import and remove; both are implemented.  Request 4 is
format-everything and is deliberately not implemented.  Remove and format
are guarded by the device with the confirmation code 0x688e.
"""

import argparse
import os
import select
import struct
import sys
import time
from pathlib import Path

from decode_stream import (
    PAYLOAD_SIZES,
    BLOCK_DISK_OPERATION,
    BLOCK_REQUEST,
    BLOCK_SUBSCRIBE,
    FRAME_SIZES,
    SIGNATURE,
    crc16_ccitt,
)
from repaper_uinput import find_serial, open_serial

DISK_REQUEST_IMPORT = 2
DISK_REQUEST_REMOVE = 3

# The device rejects remove and format without this confirmation code.
DISK_CONFIRMATION = 0x688e

BLOCK_DISK_STATUS = 0x09
BLOCK_FILE_DESCRIPTOR = 0x0a
BLOCK_FILE_DATA = 0x0b

CHUNK_HEADER_LEN = 13
IDLE_TIMEOUT = 3.0
FILE_MAGIC = SIGNATURE


def packet(block_type, payload):
    payload = bytes(payload)
    return (SIGNATURE + bytes([block_type]) + payload
            + crc16_ccitt(payload).to_bytes(2, 'little'))


def disk_import(file_id):
    """Build an import (download) request."""
    payload = struct.pack('<BQHI', DISK_REQUEST_IMPORT, 0, file_id, 0)
    return packet(BLOCK_DISK_OPERATION, payload)


def disk_remove(file_id):
    """Build a remove request.  Deletes the file from the device."""
    payload = struct.pack('<BQHI', DISK_REQUEST_REMOVE, DISK_CONFIRMATION,
                          file_id, 0)
    return packet(BLOCK_DISK_OPERATION, payload)


def remove_file(fd, file_id, progress=None, known_ids=None):
    """Delete one stored drawing, then confirm it is gone.

    Returns (ok, message).  The device sends no acknowledgement, so removal
    is verified by re-reading the file table afterwards.  Callers holding a
    current listing can pass known_ids to skip the first read, which is most
    of the wait.
    """
    def step(text, fraction):
        if progress:
            progress(text, fraction)

    if known_ids is None:
        step('checking the file table', 0.15)
        _, before = list_files(fd)
        known_ids = {entry['id'] for entry in before}
    if file_id not in known_ids:
        return False, f'file {file_id} is not on the device'

    step(f'deleting file {file_id}', 0.45)
    quiesce(fd)
    os.write(fd, disk_remove(file_id))
    time.sleep(0.5)
    drain(fd, 0.4)

    step('confirming', 0.8)
    _, after = list_files(fd)
    if file_id in {entry['id'] for entry in after}:
        return False, f'file {file_id} is still present'
    step('done', 1.0)
    return True, f'deleted file {file_id}; {len(after)} remaining'


def iter_frames(buffer):
    """Yield complete frames, leaving any partial tail in the buffer."""
    while True:
        start = buffer.find(SIGNATURE)
        if start < 0:
            del buffer[:max(len(buffer) - 2, 0)]
            return
        if start:
            del buffer[:start]
        if len(buffer) < 4:
            return
        size = FRAME_SIZES.get(buffer[3])
        if size is None:
            del buffer[:3]
            continue
        if len(buffer) < size:
            return
        frame = bytes(buffer[:size])
        del buffer[:size]
        payload = frame[4:-2]
        if crc16_ccitt(payload) == int.from_bytes(frame[-2:], 'little'):
            yield frame[3], payload


def drain(fd, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        readable, _, _ = select.select([fd], [], [], max(deadline - time.time(), 0))
        if not readable:
            break
        try:
            if not os.read(fd, 8192):
                break
        except OSError:
            break


def collect(fd, seconds, idle=None):
    """Read frames until the stream goes quiet."""
    buffer = bytearray()
    out = []
    last = time.time()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if idle and time.time() - last > idle:
            break
        readable, _, _ = select.select([fd], [], [], 0.3)
        if not readable:
            continue
        try:
            data = os.read(fd, 8192)
        except OSError:
            break
        if not data:
            continue
        last = time.time()
        buffer.extend(data)
        out.extend(iter_frames(buffer))
    return out


def quiesce(fd):
    """Silence the pen stream so downloads are not interleaved with it."""
    os.write(fd, packet(BLOCK_SUBSCRIBE, b'\x00\x00'))
    time.sleep(0.3)
    drain(fd, 0.4)


def parse_file_descriptor(payload):
    idx, size, year, month, day, hour, minute, sec = struct.unpack('<HIHBBBBB',
                                                                   payload)
    return {
        'id': idx,
        'size': size,
        'date': f'{year:04d}-{month:02d}-{day:02d}',
        'time': f'{hour:02d}:{minute:02d}:{sec:02d}',
    }


def parse_disk_status(payload):
    flags, free_space, count = struct.unpack('<BIH', payload)
    return {'flags': flags, 'free_space': free_space, 'files': count}


def list_files(fd, timeout=2.5, idle=0.6):
    """Read the disk status and file table.

    The device answers promptly, so this is bounded by the idle gap rather
    than the full timeout.  A generous idle makes every listing feel slow,
    and a delete does one on each side of the operation.
    """
    quiesce(fd)
    os.write(fd, packet(BLOCK_REQUEST, [BLOCK_DISK_STATUS]))
    os.write(fd, packet(BLOCK_REQUEST, [BLOCK_FILE_DESCRIPTOR]))
    frames = collect(fd, timeout, idle=idle)

    status = None
    files = []
    for block, payload in frames:
        if block == BLOCK_DISK_STATUS:
            status = parse_disk_status(payload)
        elif block == BLOCK_FILE_DESCRIPTOR:
            files.append(parse_file_descriptor(payload))
    return status, files


def parse_chunk_header(payload):
    """13-byte chunk header, little endian throughout.

        id:u16  reserved:u8  index:u32  total:u32  length:u16

    Both counters are 32-bit and both matter: a 43 kB file needs 676
    chunks, and reading them as 16-bit big endian happens to give the right
    answer only while the high bytes are zero.  On a large file it turns
    676 into 164 and truncates the download.  The length field matters too,
    because the last chunk is short unless the size divides by 64.
    """
    file_id, _reserved, index, total, length = struct.unpack_from(
        '<HBIIH', payload)
    return file_id, index, total, length


def download(fd, file_id, expected_size=None, progress=None):
    quiesce(fd)
    os.write(fd, disk_import(file_id))

    pieces = {}
    total = None
    buffer = bytearray()
    last = time.time()

    while time.time() - last < IDLE_TIMEOUT:
        readable, _, _ = select.select([fd], [], [], 0.3)
        if not readable:
            continue
        try:
            data = os.read(fd, 8192)
        except OSError:
            break
        if not data:
            continue
        last = time.time()
        buffer.extend(data)
        for block, payload in iter_frames(buffer):
            if block != BLOCK_FILE_DATA:
                continue
            chunk_id, index, count, length = parse_chunk_header(payload)
            if chunk_id != file_id:
                continue
            total = count
            pieces[index] = payload[CHUNK_HEADER_LEN:CHUNK_HEADER_LEN + length]
            if progress:
                progress(len(pieces), count)
        if total is not None and len(pieces) == total:
            break

    if not pieces:
        return None, 'no file-data blocks received'
    missing = [i for i in range(total or 0) if i not in pieces]
    if missing:
        return None, f'missing chunks {missing[:8]}'

    blob = b''.join(pieces[i] for i in sorted(pieces))
    if expected_size is not None and len(blob) != expected_size:
        return blob, f'size {len(blob)} != descriptor {expected_size}'
    return blob, None


# ---------------------------------------------------------------------------
# Stored file format
#
#     <signature:3> <version:u8> <record>*
#
# Each record is a block type byte followed by that block's payload, so the
# records are variable length rather than a fixed stride: type 0x03 carries
# 2 bytes and type 0x18 carries 14.  Block 0x18 appears only in stored files,
# never in the live stream, which is why probing the device never revealed it.
#
# In the 0x18 payload the first three signed 16-bit fields are x, y and the
# contact height.  The height takes exactly two values: PEN_DOWN_Z while the
# tip is on the paper, and a negative value while it is lifted.

HEADER_LEN = len(FILE_MAGIC) + 1
PEN_RECORD = 0x18
PEN_DOWN_Z = 300


def iter_records(blob):
    """Walk the records of a stored drawing, sizing each by its block type."""
    pos = HEADER_LEN
    while pos < len(blob):
        block = blob[pos]
        size = PAYLOAD_SIZES.get(block)
        if size is None:
            return
        payload = blob[pos + 1:pos + 1 + size]
        if len(payload) < size:
            return
        yield block, payload
        pos += 1 + size


def parse_drawing(blob):
    """Return a list of strokes, each a list of (x, y) points."""
    if blob[:3] != FILE_MAGIC:
        raise ValueError('not an ISKN drawing: bad signature')

    strokes = []
    current = []
    for block, payload in iter_records(blob):
        if block != PEN_RECORD:
            continue
        x, y, z = struct.unpack_from('<hhh', payload)
        if z == PEN_DOWN_Z:
            current.append((x, y))
        elif current:
            strokes.append(current)
            current = []
    if current:
        strokes.append(current)
    return strokes


def strokes_to_svg(strokes, margin=200, stroke_width=40):
    points = [p for stroke in strokes for p in stroke]
    if not points:
        return None

    min_x = min(x for x, _ in points) - margin
    max_x = max(x for x, _ in points) + margin
    min_y = min(y for _, y in points) - margin
    max_y = max(y for _, y in points) + margin
    width = max_x - min_x
    height = max_y - min_y

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{min_x} {min_y} {width} {height}" '
        f'width="{width / 100:.1f}mm" height="{height / 100:.1f}mm">',
        '<rect x="%d" y="%d" width="%d" height="%d" fill="#fdfdfb"/>'
        % (min_x, min_y, width, height),
        f'<g fill="none" stroke="#1a1a1a" stroke-width="{stroke_width}" '
        f'stroke-linecap="round" stroke-linejoin="round">',
    ]
    for stroke in strokes:
        if len(stroke) == 1:
            x, y = stroke[0]
            parts.append(f'<circle cx="{x}" cy="{y}" r="{stroke_width / 2:.0f}" '
                         f'fill="#1a1a1a" stroke="none"/>')
            continue
        d = 'M ' + ' L '.join(f'{x} {y}' for x, y in stroke)
        parts.append(f'<path d="{d}"/>')
    parts.append('</g></svg>')
    return '\n'.join(parts)


def cmd_svg(args):
    """Convert already-downloaded .iskn files to SVG. Needs no tablet."""
    failures = 0
    for name in args.svg:
        path = Path(name)
        try:
            strokes = parse_drawing(path.read_bytes())
        except (OSError, ValueError) as err:
            print(f'{path}: {err}')
            failures += 1
            continue
        svg = strokes_to_svg(strokes)
        if svg is None:
            print(f'{path}: no pen-down samples')
            failures += 1
            continue
        out = path.with_suffix('.svg')
        out.write_text(svg)
        points = sum(len(s) for s in strokes)
        print(f'{path.name}: {len(strokes)} strokes, {points} points -> {out.name}')
    return 1 if failures else 0


def format_size(value):
    return f'{value:,}'


def cmd_list(fd, args):
    status, files = list_files(fd)
    if status:
        print(f'disk: {status["files"]} files, free {format_size(status["free_space"])}, '
              f'flags 0x{status["flags"]:02x}')
    if not files:
        print('no files reported')
        return 1
    print(f'\n{"id":>3}  {"size":>9}  {"date":>10}  {"time":>8}')
    for entry in sorted(files, key=lambda f: f['id']):
        print(f'{entry["id"]:>3}  {format_size(entry["size"]):>9}  '
              f'{entry["date"]:>10}  {entry["time"]:>8}')
    print(f'\ntotal {format_size(sum(f["size"] for f in files))} bytes')
    return 0


def cmd_get(fd, args):
    _, files = list_files(fd)
    sizes = {f['id']: f['size'] for f in files}
    wanted = sorted(sizes) if args.all else args.get

    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for file_id in wanted:
        expected = sizes.get(file_id)
        label = f'file {file_id}'
        if expected:
            label += f' ({format_size(expected)} bytes)'
        print(f'{label}:')
        def show(done, total):
            print(f'\r  chunk {done}/{total}', end='', flush=True)

        blob, problem = download(fd, file_id, expected, progress=show)
        print()
        if blob is None:
            print(f'  FAILED: {problem}')
            failures += 1
            continue
        path = outdir / f'repaper-{file_id:02d}.iskn'
        path.write_bytes(blob)
        note = f'  -> {path} ({format_size(len(blob))} bytes)'
        if problem:
            note += f'  WARNING: {problem}'
        elif blob[:3] == FILE_MAGIC:
            note += '  [magic ok]'
        print(note)
    return 1 if failures else 0


def cmd_delete(fd, args):
    _, files = list_files(fd)
    known = {entry['id']: entry for entry in files}
    targets = [i for i in args.delete if i in known]
    unknown = [i for i in args.delete if i not in known]

    for missing in unknown:
        print(f'file {missing}: not on the device')
    if not targets:
        return 1

    print('About to permanently delete from the tablet:')
    for file_id in targets:
        print(f'  {file_id}: {format_size(known[file_id]["size"])} bytes')
    if not args.yes:
        answer = input('This cannot be undone. Type "delete" to confirm: ')
        if answer.strip().lower() != 'delete':
            print('Aborted.')
            return 1

    failures = 0
    for file_id in targets:
        ok, message = remove_file(fd, file_id)
        print(f'  {message}')
        failures += not ok
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(
        description='List and download drawings stored on an ISKN Repaper.')
    parser.add_argument('--serial', help='serial node, e.g. /dev/ttyACM0')
    parser.add_argument('--list', action='store_true',
                        help='show stored files')
    parser.add_argument('--get', type=int, nargs='+', metavar='ID',
                        help='download the given file ids')
    parser.add_argument('--all', action='store_true',
                        help='download every stored file')
    parser.add_argument('-o', '--output', default='drawings',
                        help='directory for downloaded files')
    parser.add_argument('--svg', nargs='+', metavar='FILE',
                        help='convert downloaded .iskn files to SVG')
    parser.add_argument('--delete', type=int, nargs='+', metavar='ID',
                        help='permanently delete files from the device')
    parser.add_argument('--yes', action='store_true',
                        help='skip the confirmation prompt for --delete')
    args = parser.parse_args()

    if args.svg:
        return cmd_svg(args)

    if not (args.list or args.get or args.all):
        args.list = True

    serial = args.serial or find_serial()
    if not serial:
        sys.exit('Cannot find the tablet. Is it plugged in and powered on?')

    fd = open_serial(serial)
    try:
        if args.delete:
            return cmd_delete(fd, args)
        if args.get or args.all:
            return cmd_get(fd, args)
        return cmd_list(fd, args)
    finally:
        try:
            os.write(fd, packet(BLOCK_SUBSCRIBE, b'\x00\x00'))
        except OSError:
            pass
        os.close(fd)


if __name__ == '__main__':
    sys.exit(main())
