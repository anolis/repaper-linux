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
    0x04: 'pen2d',
    0x09: 'disk-status',
    0x0a: 'file-descriptor',
    0x0f: 'unknown-0f',
    0x13: 'unknown-13',
    0x14: 'device-name',
    0x18: 'raw-pen3d?',
}

HEX_BYTE_RE = re.compile(r'(?<![0-9a-fA-F])(?:0x)?([0-9a-fA-F]{2})(?![0-9a-fA-F])')


def crc16_ccitt(data):
    crc = 0
    for byte in data:
        byte ^= (crc >> 8) & 0xff
        byte ^= byte >> 4
        crc = ((crc << 8) ^ (byte << 12) ^ (byte << 5) ^ byte) & 0xffff
    return crc


def bytes_from_text(text):
    return bytes(int(match, 16) for match in HEX_BYTE_RE.findall(text))


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
    if len(payload) != 14:
        return None
    return unpack_fields(
        '<hhhhhhBB',
        ('x', 'y', 'z', 'z_paper', 'rot_x', 'rot_y', 'touch', 'extra'),
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

        packet_type = data[start + 3]
        frame_len = 20 if packet_type == 0x18 else None
        if packet_type == 0x04:
            frame_len = 15
        if frame_len is None:
            # Known fixed response sizes from the vendor library's block table.
            sizes = {
                0x01: 8,
                0x02: 42,
                0x09: 13,
                0x0a: 19,
                0x0f: 18,
                0x13: 42,
                0x14: 74,
            }
            frame_len = sizes.get(packet_type)

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

    if pkt_type == 0x18:
        fields = parse_pen3d(payload)
        if fields is None:
            return
        vector_fields = (
            ('x', fields['x']),
            ('y', fields['y']),
            ('z', fields['z']),
            ('z_paper', fields['z_paper']),
            ('rot_x', fields['rot_x']),
            ('rot_y', fields['rot_y']),
        )
        print(
            f'     pen3d? raw '
            f'{format_fields((*vector_fields, ("touch", fields["touch"]), ("extra", fields["extra"])))}'
        )
        print(f'     pen3d? api {format_scaled_fields(vector_fields)} '
              f'touch={fields["touch"] != 0}')


def format_range(values):
    low = min(values)
    high = max(values)
    return (
        f'raw {low:6d}..{high:6d} range={high - low:6d}  '
        f'api {scaled(low):8.2f}..{scaled(high):8.2f} '
        f'range={scaled(high - low):7.2f}'
    )


def summarize_pen2d(frames):
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

    names = ', '.join(
        f'0x{pkt_type:02x}:{count}' for pkt_type, count in sorted(type_counts.items())
    )
    print(f'frames total={len(frames)} types={names} bad_crc={bad_crc}')

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
    if transitions:
        for index, before, after in transitions:
            print(f'  {index:04d}: {before} -> {after}')
    else:
        print('  none')


def main():
    parser = argparse.ArgumentParser(
        description='Decode ISKN serial stream packets from hex text.',
    )
    parser.add_argument(
        '--summary',
        action='store_true',
        help='print compact pen2d ranges and touch transitions instead of every frame',
    )
    args = parser.parse_args()

    data = bytes_from_text(sys.stdin.read())
    frames = list(iter_frames(data))
    if not frames:
        print(f'No ISKN frames found in {len(data)} parsed hex bytes.', file=sys.stderr)
        print('Expected input containing bytes like: b3 a5 e1 04 ...', file=sys.stderr)
        return 1

    if args.summary:
        summarize_pen2d(frames)
        return 0

    for index, frame in enumerate(frames, start=1):
        describe_frame(frame, index)
    return 0


if __name__ == '__main__':
    sys.exit(main())
