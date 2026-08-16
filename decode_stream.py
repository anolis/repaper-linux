#!/usr/bin/env python3
"""
Decode ISKN serial stream packets from hex text.

The live pen stream appears as framed 20-byte packets:

    b3 a5 e1 04 <9-byte payload> <crc16-le>
    b3 a5 e1 18 <14-byte payload> <crc16-le>

This script extracts frames from stdin and prints the packet type, CRC check,
and payload interpreted as signed/unsigned little-endian 16-bit words.
"""

import argparse
import re
import struct
import sys

SIGNATURE = bytes([0xb3, 0xa5, 0xe1])
VENDOR_SCALE = 0.01

PACKET_NAMES = {
    0x01: 'status',
    0x02: 'description',
    0x03: 'unknown-03',
    0x04: 'pen2d',
    0x05: 'pen3d',
    0x06: 'raw3d',
    0x09: 'disk-status',
    0x0a: 'file-descriptor',
    0x0f: 'unknown-0f',
    0x13: 'unknown-13',
    0x14: 'device-name',
    0x18: 'raw-pen3d?',
}

# Payload sizes for blocks 0x01..0x0d come from the table that
# BlockManager::InitDataBlockSize() builds in libISKN_API.so.1.0.0, and every
# one of them matches a frame captured from the hardware.  0x0f, 0x13 and
# 0x14 are not in that table and were measured directly.
PAYLOAD_SIZES = {
    0x01: 2,    # status
    0x02: 36,   # description
    0x03: 2,
    0x04: 9,    # pen2d
    0x05: 13,   # pen3d
    0x06: 10,   # raw3d
    0x07: 13,
    0x08: 1,
    0x09: 7,    # disk status
    0x0a: 13,   # file descriptor
    0x0b: 77,   # file data chunk
    0x0c: 9,
    0x0d: 5,
    0x18: 14,   # stored-file pen record; never seen in the live stream
}

# Frame sizes include the signature, block type and CRC.
FRAME_SIZES = {block: size + 6 for block, size in PAYLOAD_SIZES.items()}
FRAME_SIZES.update({
    0x0f: 18,
    0x13: 42,
    0x14: 74,
})

# Blocks the computer sends to the device.
BLOCK_SUBSCRIBE = 0x33
BLOCK_REQUEST = 0x34
BLOCK_DISK_OPERATION = 0x35
BLOCK_SET_TIME = 0x36
BLOCK_SET_PIN = 0x37
BLOCK_SET_DEVICE_NAME = 0x38

# The subscribe payload (block 0x33) is a 16-bit bitmask, not a stream id:
# bit N enables the auto-block whose type is AUTO_BLOCK_BASE + N.
AUTO_BLOCK_BASE = 0x02


def auto_block_mask(*block_types):
    """Bitmask that subscribes to exactly the given auto-block types."""
    mask = 0
    for block_type in block_types:
        mask |= 1 << (block_type - AUTO_BLOCK_BASE)
    return mask

HEX_TOKEN_RE = re.compile(r'^(?:0x)?([0-9a-fA-F]{2})$')

# A real dump always shows several bytes back to back, so require a run of at
# least this many before believing them.
MIN_HEX_RUN = 2


def crc16_ccitt(data):
    crc = 0
    for byte in data:
        byte ^= (crc >> 8) & 0xff
        byte ^= byte >> 4
        crc = ((crc << 8) ^ (byte << 12) ^ (byte << 5) ^ byte) & 0xffff
    return crc


def bytes_from_text(text):
    """Scrape hex byte dumps out of probe/trace log text.

    Log lines interleave dumps with values that merely look like hex, e.g.
    ``[trace] read 15: b3 a5 e1 04`` where ``15`` is a byte count, or
    ``/dev/ttyACM0`` which hides an ``AC``.  Accept only whole tokens that are
    exactly one hex byte, and only where several appear consecutively.
    """
    collected = bytearray()

    def flush(run):
        if len(run) >= MIN_HEX_RUN:
            collected.extend(run)

    for line in text.splitlines():
        run = []
        for token in line.split():
            match = HEX_TOKEN_RE.match(token)
            if match:
                run.append(int(match.group(1), 16))
                continue
            flush(run)
            run = []
        flush(run)

    return bytes(collected)


def scaled(value):
    return value * VENDOR_SCALE


def format_fields(fields):
    return ' '.join(f'{name}={value}' for name, value in fields)


def format_scaled_fields(fields):
    return ' '.join(f'{name}={scaled(value):.2f}' for name, value in fields)


def unpack_fields(fmt, names, payload):
    return dict(zip(names, struct.unpack(fmt, payload)))


def packet_type(frame):
    return frame[3]


def frame_payload(frame):
    return frame[4:-2]


def crc_state(frame):
    payload = frame_payload(frame)
    expected_crc = int.from_bytes(frame[-2:], 'little')
    actual_crc = crc16_ccitt(payload)
    return expected_crc == actual_crc, expected_crc, actual_crc


def parse_pen2d(payload):
    if len(payload) != 9:
        return None
    return unpack_fields('<hhhhB', ('x', 'y', 'rot_x', 'rot_y', 'state'), payload)


def parse_pen3d(payload):
    """Block 0x05: pen2d plus a height and a frame counter.

    Field 4 was previously read as a second height ('z_paper').  It is a
    sequence counter that advances by 2 every frame regardless of the pen,
    so it is unsigned and must not be scaled as a coordinate.
    """
    if len(payload) != 13:
        return None
    return unpack_fields(
        '<hhhHhhB',
        ('x', 'y', 'z', 'seq', 'rot_x', 'rot_y', 'state'),
        payload,
    )


def parse_raw3d(payload):
    """Block 0x06: three coordinates plus the orientation vector."""
    if len(payload) != 10:
        return None
    return unpack_fields(
        '<hhhhh',
        ('x', 'y', 'z', 'rot_x', 'rot_y'),
        payload,
    )


def iter_frames(data):
    idx = 0
    while True:
        start = data.find(SIGNATURE, idx)
        if start < 0:
            return
        if start + 4 > len(data):
            return

        frame_len = FRAME_SIZES.get(data[start + 3])
        if frame_len is None or start + frame_len > len(data):
            idx = start + 3
            continue

        yield data[start:start + frame_len]
        idx = start + frame_len


def describe_frame(frame, index):
    pkt_type = packet_type(frame)
    payload = frame_payload(frame)
    crc_ok, _, actual_crc = crc_state(frame)
    crc_label = 'ok' if crc_ok else f'bad:{actual_crc:04x}'

    packet_name = PACKET_NAMES.get(pkt_type, 'unknown')
    print(f'{index:04d} type=0x{pkt_type:02x} {packet_name} '
          f'payload={len(payload)} crc={crc_label}')
    print(f'     raw={payload.hex(" ")}')

    words_payload = payload[:-1] if len(payload) % 2 == 1 else payload
    if len(words_payload) >= 2:
        unsigned = struct.unpack('<' + 'H' * (len(words_payload) // 2), words_payload)
        signed = struct.unpack('<' + 'h' * (len(words_payload) // 2), words_payload)
        print('     u16=' + ' '.join(f'{value:5d}' for value in unsigned))
        print('     s16=' + ' '.join(f'{value:5d}' for value in signed))

    if pkt_type == 0x04:
        fields = parse_pen2d(payload)
        if fields is None:
            return
        vector_fields = (
            ('x', fields['x']),
            ('y', fields['y']),
            ('rot_x', fields['rot_x']),
            ('rot_y', fields['rot_y']),
        )
        print(f'     pen2d raw {format_fields((*vector_fields, ("state", fields["state"])))}')
        print(f'     pen2d api {format_scaled_fields(vector_fields)} '
              f'touch={fields["state"] != 0}')

    if pkt_type == 0x05:
        fields = parse_pen3d(payload)
        if fields is None:
            return
        vector_fields = (
            ('x', fields['x']),
            ('y', fields['y']),
            ('z', fields['z']),
        )
        print(f'     pen3d raw {format_fields((*vector_fields, ("seq", fields["seq"]), ("rot_x", fields["rot_x"]), ("rot_y", fields["rot_y"]), ("state", fields["state"])))}')
        print(f'     pen3d api {format_scaled_fields(vector_fields)} '
              f'touch={fields["state"] != 0}')

    if pkt_type == 0x06:
        fields = parse_raw3d(payload)
        if fields is None:
            return
        vector_fields = (
            ('x', fields['x']),
            ('y', fields['y']),
            ('z', fields['z']),
        )
        print(f'     raw3d raw {format_fields((*vector_fields, ("rot_x", fields["rot_x"]), ("rot_y", fields["rot_y"])))}')
        print(f'     raw3d api {format_scaled_fields(vector_fields)}')


def format_range(values):
    low = min(values)
    high = max(values)
    return (
        f'raw {low:6d}..{high:6d} range={high - low:6d}  '
        f'api {scaled(low):8.2f}..{scaled(high):8.2f} '
        f'range={scaled(high - low):7.2f}'
    )


def collect_pen2d_stats(frames):
    samples = []
    bad_crc = 0
    type_counts = {}
    transitions = []
    previous_touch = None

    for index, frame in enumerate(frames, start=1):
        pkt_type = packet_type(frame)
        type_counts[pkt_type] = type_counts.get(pkt_type, 0) + 1
        crc_ok, _, _ = crc_state(frame)
        if not crc_ok:
            bad_crc += 1

        if pkt_type != 0x04:
            continue

        fields = parse_pen2d(frame_payload(frame))
        if fields is None:
            continue

        touch = fields['state'] != 0
        if previous_touch is not None and touch != previous_touch:
            transitions.append((index, previous_touch, touch))
        previous_touch = touch

        samples.append({
            'index': index,
            'crc_ok': crc_ok,
            'touch': touch,
            **fields,
        })

    return {
        'bad_crc': bad_crc,
        'samples': samples,
        'transitions': transitions,
        'type_counts': type_counts,
    }


def summarize_pen2d(frames):
    stats = collect_pen2d_stats(frames)
    samples = stats['samples']
    names = ', '.join(
        f'0x{pkt_type:02x}:{count}'
        for pkt_type, count in sorted(stats['type_counts'].items())
    )
    print(f'frames total={len(frames)} types={names} bad_crc={stats["bad_crc"]}')

    if not samples:
        print('pen2d frames=0')
        return

    touch_count = sum(1 for sample in samples if sample['touch'])
    print(
        f'pen2d frames={len(samples)} hover={len(samples) - touch_count} '
        f'touch={touch_count} states={",".join(str(state) for state in sorted({s["state"] for s in samples}))}'
    )

    for name in ('x', 'y', 'rot_x', 'rot_y'):
        print(f'{name:5s} {format_range([sample[name] for sample in samples])}')

    print('touch transitions:')
    if stats['transitions']:
        for index, before, after in stats['transitions']:
            print(f'  {index:04d}: {before} -> {after}')
    else:
        print('  none')


def print_bounds(frames):
    stats = collect_pen2d_stats(frames)
    samples = stats['samples']
    if not samples:
        print('No pen2d frames found.', file=sys.stderr)
        return 1

    bounds = {
        name: (min(sample[name] for sample in samples),
               max(sample[name] for sample in samples))
        for name in ('x', 'y', 'rot_x', 'rot_y')
    }
    touch_count = sum(1 for sample in samples if sample['touch'])

    print('# ISKN Repaper pen2d calibration bounds')
    print(f'# frames={len(frames)} pen2d_frames={len(samples)} bad_crc={stats["bad_crc"]}')
    print(f'# hover_frames={len(samples) - touch_count} touch_frames={touch_count}')
    for name, (low, high) in bounds.items():
        upper_name = name.upper()
        print(f'REPAPER_{upper_name}_MIN={low}')
        print(f'REPAPER_{upper_name}_MAX={high}')
    print(f'REPAPER_TOUCH_STATES={",".join(str(state) for state in sorted({s["state"] for s in samples}))}')
    return 0


def read_input(paths):
    if not paths:
        return bytes_from_text(sys.stdin.read())

    chunks = []
    for path in paths:
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                chunks.append(bytes_from_text(handle.read()))
        except OSError as err:
            print(f'{path}: {err}', file=sys.stderr)
            return None
    return b''.join(chunks)


def main():
    parser = argparse.ArgumentParser(
        description='Decode ISKN serial stream packets from hex text.',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        '--bounds',
        action='store_true',
        help='print reusable pen2d min/max calibration values',
    )
    mode.add_argument(
        '--summary',
        action='store_true',
        help='print compact pen2d ranges and touch transitions instead of every frame',
    )
    parser.add_argument(
        'paths',
        nargs='*',
        help='optional trace/probe log files; stdin is used when omitted',
    )
    args = parser.parse_args()

    data = read_input(args.paths)
    if data is None:
        return 1

    frames = list(iter_frames(data))
    if not frames:
        print(f'No ISKN frames found in {len(data)} parsed hex bytes.', file=sys.stderr)
        print('Expected input containing bytes like: b3 a5 e1 04 ...', file=sys.stderr)
        return 1

    if args.summary:
        summarize_pen2d(frames)
        return 0

    if args.bounds:
        return print_bounds(frames)

    for index, frame in enumerate(frames, start=1):
        describe_frame(frame, index)
    return 0


if __name__ == '__main__':
    sys.exit(main())
