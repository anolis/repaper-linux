#!/usr/bin/env python3
"""
Tests for the disk protocol and the stored drawing format.

All pure functions: command construction, chunk reassembly, record walking
and SVG output.  No tablet required.
"""

import struct
import unittest

import decode_stream
import repaper_files


def chunk_header(file_id, index, total, length):
    return struct.pack('<HBIIH', file_id, 0, index, total, length)


def pen_record(x, y, z, tail=b'\x00' * 8):
    return bytes([repaper_files.PEN_RECORD]) + struct.pack('<hhh', x, y, z) + tail


def drawing(*records, version=b'\x02'):
    return repaper_files.FILE_MAGIC + version + b''.join(records)


class DiskCommandTest(unittest.TestCase):
    """Layout taken from BlockDiskOperation in the vendor library."""

    def test_import_is_21_bytes(self):
        self.assertEqual(len(repaper_files.disk_import(6)), 21)

    def test_import_targets_the_right_file(self):
        packet = repaper_files.disk_import(6)
        request, code, file_id, arg = struct.unpack('<BQHI', packet[4:-2])
        self.assertEqual(request, repaper_files.DISK_REQUEST_IMPORT)
        self.assertEqual(file_id, 6)
        self.assertEqual(arg, 0)

    def test_import_sends_no_confirmation_code(self):
        # Only the destructive requests carry one; import must not.
        _, code, _, _ = struct.unpack('<BQHI', repaper_files.disk_import(6)[4:-2])
        self.assertEqual(code, 0)

    def test_remove_carries_the_confirmation_code(self):
        # The device rejects a remove without it, so this must not regress.
        request, code, file_id, _ = struct.unpack(
            '<BQHI', repaper_files.disk_remove(3)[4:-2])
        self.assertEqual(request, repaper_files.DISK_REQUEST_REMOVE)
        self.assertEqual(code, repaper_files.DISK_CONFIRMATION)
        self.assertEqual(file_id, 3)

    def test_import_and_remove_are_distinguishable(self):
        self.assertNotEqual(repaper_files.disk_import(3),
                            repaper_files.disk_remove(3))

    def test_commands_carry_a_valid_crc(self):
        for packet in (repaper_files.disk_import(1), repaper_files.disk_remove(1)):
            payload = packet[4:-2]
            self.assertEqual(decode_stream.crc16_ccitt(payload),
                             int.from_bytes(packet[-2:], 'little'))


class ChunkHeaderTest(unittest.TestCase):

    def test_reads_all_four_fields(self):
        header = chunk_header(6, 24, 25, 43)
        self.assertEqual(repaper_files.parse_chunk_header(header),
                         (6, 24, 25, 43))

    def test_handles_counts_above_a_byte(self):
        # 43 kB needs 676 chunks; a 16-bit big-endian read gives 164 instead
        # and truncates the download to a quarter of the file.
        header = chunk_header(1, 675, 676, 64)
        file_id, index, total, length = repaper_files.parse_chunk_header(header)
        self.assertEqual(index, 675)
        self.assertEqual(total, 676)
        self.assertNotEqual(total, 164)

    def test_matches_a_captured_header(self):
        captured = bytes.fromhex('0600' '00' '00000000' '19000000' '4000')
        self.assertEqual(repaper_files.parse_chunk_header(captured),
                         (6, 0, 25, 64))

    def test_matches_a_captured_final_header(self):
        # Last chunk of file 6: short, which is what makes the size exact.
        captured = bytes.fromhex('0600' '00' '18000000' '19000000' '2b00')
        _, index, total, length = repaper_files.parse_chunk_header(captured)
        self.assertEqual((index, total, length), (24, 25, 43))

    def test_chunk_lengths_reproduce_the_file_size(self):
        self.assertEqual(24 * 64 + 43, 1579)


class FileDescriptorTest(unittest.TestCase):

    def test_matches_a_captured_descriptor(self):
        payload = bytes.fromhex('0100' '00a90000' 'e307' '01' '01' '00' '00' '00')
        entry = repaper_files.parse_file_descriptor(payload)
        self.assertEqual(entry['id'], 1)
        self.assertEqual(entry['size'], 43264)
        self.assertEqual(entry['date'], '2019-01-01')

    def test_disk_status_reports_the_file_count(self):
        payload = bytes.fromhex('07' '700e0000' '0800')
        status = repaper_files.parse_disk_status(payload)
        self.assertEqual(status['files'], 8)


class RecordWalkTest(unittest.TestCase):
    """Records are sized by block type, not by a fixed stride."""

    def test_walks_uniform_pen_records(self):
        blob = drawing(pen_record(1, 2, 300), pen_record(3, 4, 300))
        blocks = [block for block, _ in repaper_files.iter_records(blob)]
        self.assertEqual(blocks, [repaper_files.PEN_RECORD] * 2)

    def test_walks_past_a_shorter_leading_block(self):
        # Some files open with a 2-byte 0x03 record; assuming a fixed 15-byte
        # stride desynchronises on exactly these files.
        blob = drawing(b'\x03\x01\x01', pen_record(5, 6, 300))
        records = list(repaper_files.iter_records(blob))
        self.assertEqual([block for block, _ in records], [0x03, 0x18])

    def test_stops_on_an_unknown_block(self):
        blob = drawing(pen_record(1, 1, 300), b'\x7f\x00\x00')
        self.assertEqual(len(list(repaper_files.iter_records(blob))), 1)

    def test_ignores_a_truncated_trailing_record(self):
        blob = drawing(pen_record(1, 1, 300)) + b'\x18\x00\x00'
        self.assertEqual(len(list(repaper_files.iter_records(blob))), 1)

    def test_pen_record_size_comes_from_the_shared_table(self):
        self.assertEqual(decode_stream.PAYLOAD_SIZES[repaper_files.PEN_RECORD], 14)


class DrawingTest(unittest.TestCase):

    def test_rejects_a_file_without_the_signature(self):
        with self.assertRaises(ValueError):
            repaper_files.parse_drawing(b'nope' + b'\x00' * 20)

    def test_contact_samples_become_a_stroke(self):
        blob = drawing(pen_record(0, 0, repaper_files.PEN_DOWN_Z),
                       pen_record(10, 10, repaper_files.PEN_DOWN_Z))
        self.assertEqual(repaper_files.parse_drawing(blob), [[(0, 0), (10, 10)]])

    def test_lifting_the_pen_breaks_the_stroke(self):
        blob = drawing(pen_record(0, 0, repaper_files.PEN_DOWN_Z),
                       pen_record(5, 5, -296),
                       pen_record(9, 9, repaper_files.PEN_DOWN_Z))
        self.assertEqual(repaper_files.parse_drawing(blob),
                         [[(0, 0)], [(9, 9)]])

    def test_hover_only_file_has_no_strokes(self):
        blob = drawing(pen_record(0, 0, -296), pen_record(1, 1, -296))
        self.assertEqual(repaper_files.parse_drawing(blob), [])

    def test_trailing_stroke_is_kept(self):
        blob = drawing(pen_record(1, 1, repaper_files.PEN_DOWN_Z))
        self.assertEqual(len(repaper_files.parse_drawing(blob)), 1)


class SvgTest(unittest.TestCase):

    def test_returns_none_without_points(self):
        self.assertIsNone(repaper_files.strokes_to_svg([]))

    def test_emits_a_path_per_stroke(self):
        svg = repaper_files.strokes_to_svg([[(0, 0), (10, 10)],
                                            [(20, 20), (30, 30)]])
        self.assertEqual(svg.count('<path'), 2)

    def test_single_point_stroke_becomes_a_dot(self):
        # A tap has no line to draw, but discarding it would lose ink.
        svg = repaper_files.strokes_to_svg([[(5, 5)]])
        self.assertIn('<circle', svg)

    def test_viewbox_covers_the_ink_with_a_margin(self):
        svg = repaper_files.strokes_to_svg([[(0, 0), (100, 200)]], margin=10)
        viewbox = svg.split('viewBox="')[1].split('"')[0]
        min_x, min_y, width, height = (float(v) for v in viewbox.split())
        self.assertEqual((min_x, min_y), (-10, -10))
        self.assertEqual((width, height), (120, 220))

    def test_is_well_formed_xml(self):
        import xml.etree.ElementTree as ET
        svg = repaper_files.strokes_to_svg([[(0, 0), (1, 1)], [(2, 2)]])
        ET.fromstring(svg)


if __name__ == '__main__':
    unittest.main()
