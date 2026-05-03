#!/usr/bin/env python3
"""
Decode ISKN serial stream packets from hex text.

The live pen stream appears as framed 20-byte packets:

    b3 a5 e1 04 <9-byte payload> <crc16-le>
    b3 a5 e1 18 <14-byte payload> <crc16-le>

This script extracts frames from stdin and prints the packet type, CRC check,
and payload interpreted as signed/unsigned little-endian 16-bit words.
"""

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


def crc16_ccitt(data):
    crc = 0
    for byte in data:
        byte ^= (crc >> 8) & 0xff
        byte ^= byte >> 4
        crc = ((crc << 8) ^ (byte << 12) ^ (byte << 5) ^ byte) & 0xffff
    return crc


def bytes_from_text(text):
    return bytes(int(match, 16) for match in re.findall(r'\b[0-9a-fA-F]{2}\b', text))


def scaled(value):
    return value * VENDOR_SCALE


def format_fields(fields):
    return ' '.join(f'{name}={value}' for name, value in fields)


def format_scaled_fields(fields):
    return ' '.join(f'{name}={scaled(value):.2f}' for name, value in fields)


def unpack_fields(fmt, names, payload):
    return dict(zip(names, struct.unpack(fmt, payload)))


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
    packet_type = frame[3]
    payload = frame[4:-2]
    expected_crc = int.from_bytes(frame[-2:], 'little')
    actual_crc = crc16_ccitt(payload)
    crc_state = 'ok' if expected_crc == actual_crc else f'bad:{actual_crc:04x}'

    packet_name = PACKET_NAMES.get(packet_type, 'unknown')
    print(f'{index:04d} type=0x{packet_type:02x} {packet_name} '
          f'payload={len(payload)} crc={crc_state}')
    print(f'     raw={payload.hex(" ")}')

    words_payload = payload[:-1] if len(payload) % 2 == 1 else payload
    if len(words_payload) >= 2:
        unsigned = struct.unpack('<' + 'H' * (len(words_payload) // 2), words_payload)
        signed = struct.unpack('<' + 'h' * (len(words_payload) // 2), words_payload)
        print('     u16=' + ' '.join(f'{value:5d}' for value in unsigned))
        print('     s16=' + ' '.join(f'{value:5d}' for value in signed))

    if packet_type == 0x04 and len(payload) == 9:
        fields = unpack_fields('<hhhhB', ('x', 'y', 'rot_x', 'rot_y', 'state'),
                               payload)
        vector_fields = (
            ('x', fields['x']),
            ('y', fields['y']),
            ('rot_x', fields['rot_x']),
            ('rot_y', fields['rot_y']),
        )
        print(f'     pen2d raw {format_fields((*vector_fields, ("state", fields["state"])))}')
        print(f'     pen2d api {format_scaled_fields(vector_fields)} '
              f'touch={fields["state"] != 0}')

    if packet_type == 0x18 and len(payload) == 14:
        fields = unpack_fields(
            '<hhhhhhBB',
            ('x', 'y', 'z', 'z_paper', 'rot_x', 'rot_y', 'touch', 'extra'),
            payload,
        )
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


def main():
    data = bytes_from_text(sys.stdin.read())
    for index, frame in enumerate(iter_frames(data), start=1):
        describe_frame(frame, index)


if __name__ == '__main__':
    main()
