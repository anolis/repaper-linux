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


def crc16_ccitt(data):
    crc = 0
    for byte in data:
        byte ^= (crc >> 8) & 0xff
        byte ^= byte >> 4
        crc = ((crc << 8) ^ (byte << 12) ^ (byte << 5) ^ byte) & 0xffff
    return crc


def bytes_from_text(text):
    return bytes(int(match, 16) for match in re.findall(r'\b[0-9a-fA-F]{2}\b', text))


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

    print(f'{index:04d} type=0x{packet_type:02x} payload={len(payload)} crc={crc_state}')
    print(f'     raw={payload.hex(" ")}')

    words_payload = payload[:-1] if len(payload) % 2 == 1 else payload
    if len(words_payload) >= 2:
        unsigned = struct.unpack('<' + 'H' * (len(words_payload) // 2), words_payload)
        signed = struct.unpack('<' + 'h' * (len(words_payload) // 2), words_payload)
        print('     u16=' + ' '.join(f'{value:5d}' for value in unsigned))
        print('     s16=' + ' '.join(f'{value:5d}' for value in signed))

    if packet_type == 0x04 and len(payload) == 9:
        a_raw, b_raw, c_raw, d_raw, state = struct.unpack('<hhhhB', payload)
        print(f'     pen2d? a={a_raw} b={b_raw} c={c_raw} d={d_raw} state={state}')

    if packet_type == 0x18 and len(payload) == 14:
        x_raw, y_raw, z_raw, t_raw, a_raw, b_raw, pressure = struct.unpack('<hhhhhhH',
                                                                           payload)
        print(f'     pen? x={x_raw} y={y_raw} z={z_raw} t={t_raw} '
              f'a={a_raw} b={b_raw} pressure={pressure}')


def main():
    data = bytes_from_text(sys.stdin.read())
    for index, frame in enumerate(iter_frames(data), start=1):
        describe_frame(frame, index)


if __name__ == '__main__':
    main()
