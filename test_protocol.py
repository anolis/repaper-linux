#!/usr/bin/env python3
"""
Tests for the ISKN protocol logic that does not need the tablet attached.

Everything here is pure functions: checksum, frame extraction, payload layout
and the orientation transform.  Run with:

    python3 -m unittest discover -p 'test_*.py'
"""

import unittest

import decode_stream
import repaper_uinput


def frame(block_type, payload):
    """Build a well-formed frame the way the device does."""
    return repaper_uinput.iskn_packet(block_type, payload)


def pen2d_frame(x, y, rot_x, rot_y, state):
    import struct
    return frame(0x04, struct.pack('<hhhhB', x, y, rot_x, rot_y, state))


class Crc16Test(unittest.TestCase):
    """The vendor checksum is CRC-16/XMODEM: CCITT polynomial, init 0."""

    def test_known_check_vector(self):
        # The standard CRC-16/XMODEM check value for b'123456789'.
        self.assertEqual(decode_stream.crc16_ccitt(b'123456789'), 0x31C3)

    def test_empty_input_is_zero(self):
        self.assertEqual(decode_stream.crc16_ccitt(b''), 0x0000)

    def test_decoder_and_bridge_agree(self):
        payload = bytes(range(20))
        self.assertEqual(decode_stream.crc16_ccitt(payload),
                         repaper_uinput.crc16_ccitt(payload))

    def test_crc_covers_payload_only_not_block_type(self):
        # Both sides checksum the payload and exclude the block type byte;
        # if that ever diverges, every frame silently fails validation.
        payload = b'\x01\x02\x03'
        built = repaper_uinput.iskn_packet(0x33, payload)
        self.assertEqual(int.from_bytes(built[-2:], 'little'),
                         decode_stream.crc16_ccitt(payload))


class PacketBuildTest(unittest.TestCase):

    def test_signature_and_block_type(self):
        built = repaper_uinput.iskn_packet(0x34, [0x02])
        self.assertEqual(built[:3], bytes([0xb3, 0xa5, 0xe1]))
        self.assertEqual(built[3], 0x34)

    def test_length_is_signature_type_payload_crc(self):
        built = repaper_uinput.iskn_packet(0x33, b'\x00\x00')
        self.assertEqual(len(built), 3 + 1 + 2 + 2)

    def test_round_trips_through_the_decoder(self):
        built = pen2d_frame(100, -200, 3, -4, 1)
        frames = list(decode_stream.iter_frames(built))
        self.assertEqual(len(frames), 1)
        crc_ok, _, _ = decode_stream.crc_state(frames[0])
        self.assertTrue(crc_ok)


class AutoBlockMaskTest(unittest.TestCase):
    """Subscribe (block 0x33) takes a bitmask, not a stream id.

    Bit N enables auto-block 0x02 + N.  Sending a small integer as though it
    were an id silently selects whichever bits that integer happens to set.
    """

    def test_single_blocks_map_to_single_bits(self):
        self.assertEqual(decode_stream.auto_block_mask(0x03), 0x0002)
        self.assertEqual(decode_stream.auto_block_mask(0x04), 0x0004)
        self.assertEqual(decode_stream.auto_block_mask(0x05), 0x0008)
        self.assertEqual(decode_stream.auto_block_mask(0x06), 0x0010)

    def test_masks_combine(self):
        self.assertEqual(decode_stream.auto_block_mask(0x05, 0x06), 0x0018)

    def test_combined_mask_is_order_independent(self):
        self.assertEqual(decode_stream.auto_block_mask(0x04, 0x06),
                         decode_stream.auto_block_mask(0x06, 0x04))

    def test_old_id_style_subscribe_was_ambiguous(self):
        # The historical loop sent ids 0..5 verbatim.  Value 5 is bits 0 and 2,
        # so it requested block 0x02 and pen2d together rather than "stream 5".
        self.assertEqual(decode_stream.auto_block_mask(0x02, 0x04), 5)


class FrameSizeTableTest(unittest.TestCase):

    def test_decoder_and_bridge_tables_match(self):
        for pkt_type, size in decode_stream.FRAME_SIZES.items():
            self.assertEqual(repaper_uinput.FRAME_SIZES.get(pkt_type), size,
                             f'size mismatch for block 0x{pkt_type:02x}')

    def test_streaming_block_sizes_fit_their_payloads(self):
        # pen2d 9B, pen3d 13B, raw3d 10B, each plus 3B signature, 1B type, 2B CRC.
        for pkt_type, payload_len in ((0x04, 9), (0x05, 13), (0x06, 10)):
            self.assertEqual(decode_stream.FRAME_SIZES[pkt_type],
                             payload_len + 6)


class DecoderFramingTest(unittest.TestCase):

    def test_extracts_a_single_pen2d_frame(self):
        data = pen2d_frame(1, 2, 3, 4, 0)
        self.assertEqual(len(list(decode_stream.iter_frames(data))), 1)

    def test_skips_leading_garbage(self):
        data = b'\xde\xad\xbe\xef' + pen2d_frame(1, 2, 3, 4, 0)
        frames = list(decode_stream.iter_frames(data))
        self.assertEqual(len(frames), 1)
        self.assertEqual(decode_stream.packet_type(frames[0]), 0x04)

    def test_extracts_back_to_back_frames(self):
        data = pen2d_frame(1, 2, 3, 4, 0) + pen2d_frame(5, 6, 7, 8, 1)
        frames = list(decode_stream.iter_frames(data))
        self.assertEqual(len(frames), 2)

    def test_ignores_truncated_trailing_frame(self):
        data = pen2d_frame(1, 2, 3, 4, 0) + pen2d_frame(5, 6, 7, 8, 1)[:6]
        self.assertEqual(len(list(decode_stream.iter_frames(data))), 1)

    def test_unknown_block_type_does_not_consume_a_later_frame(self):
        # A signature with an unknown type must resync, not swallow what follows.
        data = bytes([0xb3, 0xa5, 0xe1, 0x7f]) + pen2d_frame(9, 9, 9, 9, 1)
        frames = list(decode_stream.iter_frames(data))
        self.assertEqual(len(frames), 1)
        self.assertEqual(decode_stream.packet_type(frames[0]), 0x04)

    def test_detects_a_corrupted_payload(self):
        data = bytearray(pen2d_frame(1, 2, 3, 4, 0))
        data[5] ^= 0xff
        built = list(decode_stream.iter_frames(bytes(data)))[0]
        crc_ok, _, _ = decode_stream.crc_state(built)
        self.assertFalse(crc_ok)


class HexScrapeTest(unittest.TestCase):
    """The decoder reads hex out of probe/trace logs, not raw binary."""

    def test_reads_trace_style_lines(self):
        text = '[trace] read 15: b3 a5 e1 04 01 00'
        self.assertEqual(decode_stream.bytes_from_text(text),
                         bytes([0xb3, 0xa5, 0xe1, 0x04, 0x01, 0x00]))

    def test_ignores_longer_hex_runs(self):
        # Decimal counts and long hex blobs must not be mistaken for bytes.
        self.assertEqual(decode_stream.bytes_from_text('deadbeef'), b'')

    def test_accepts_0x_prefixed_bytes(self):
        self.assertEqual(decode_stream.bytes_from_text('0xb3 0xa5'),
                         bytes([0xb3, 0xa5]))


class Pen2dPayloadTest(unittest.TestCase):

    def test_field_order_and_signedness(self):
        built = pen2d_frame(-1000, 2000, -30, 40, 1)
        fields = decode_stream.parse_pen2d(decode_stream.frame_payload(built))
        self.assertEqual(fields, {'x': -1000, 'y': 2000,
                                  'rot_x': -30, 'rot_y': 40, 'state': 1})

    def test_rejects_wrong_length(self):
        self.assertIsNone(decode_stream.parse_pen2d(b'\x00' * 8))

    def test_bridge_and_decoder_agree_on_layout(self):
        built = pen2d_frame(123, -456, 7, -8, 1)
        bridge = repaper_uinput.parse_pen(built)
        decoder = decode_stream.parse_pen2d(decode_stream.frame_payload(built))
        self.assertEqual(bridge['x'], decoder['x'])
        self.assertEqual(bridge['y'], decoder['y'])
        self.assertEqual(bridge['rot_x'], decoder['rot_x'])
        self.assertEqual(bridge['rot_y'], decoder['rot_y'])
        self.assertEqual(bridge['touch'], decoder['state'] != 0)

    def test_bridge_rejects_bad_crc(self):
        data = bytearray(pen2d_frame(1, 2, 3, 4, 0))
        data[-1] ^= 0xff
        self.assertIsNone(repaper_uinput.parse_pen(bytes(data)))

    def test_pen2d_reports_no_height(self):
        sample = repaper_uinput.parse_pen(pen2d_frame(1, 2, 3, 4, 0))
        self.assertIsNone(sample['z'])


class Pen3dPayloadTest(unittest.TestCase):
    """pen3d (0x05) is pen2d plus a height and a frame counter."""

    def build(self, x, y, z, seq, rot_x, rot_y, state):
        import struct
        return frame(0x05, struct.pack('<hhhHhhB', x, y, z, seq,
                                       rot_x, rot_y, state))

    def test_field_order(self):
        sample = repaper_uinput.parse_pen(
            self.build(-100, 200, 300, 4242, -5, 6, 1))
        self.assertEqual(sample['x'], -100)
        self.assertEqual(sample['y'], 200)
        self.assertEqual(sample['z'], 300)
        self.assertEqual(sample['seq'], 4242)
        self.assertEqual(sample['rot_x'], -5)
        self.assertEqual(sample['rot_y'], 6)
        self.assertTrue(sample['touch'])

    def test_sequence_field_is_unsigned(self):
        # It counts up by 2 per frame and wraps, so it must not go negative.
        sample = repaper_uinput.parse_pen(
            self.build(0, 0, 0, 65534, 0, 0, 0))
        self.assertEqual(sample['seq'], 65534)

    def test_frame_is_longer_than_pen2d(self):
        self.assertGreater(len(self.build(0, 0, 0, 0, 0, 0, 0)),
                           len(pen2d_frame(0, 0, 0, 0, 0)))

    def test_rejects_wrong_length(self):
        self.assertIsNone(repaper_uinput.parse_pen(frame(0x05, b'\x00' * 9)))


class BufferFramingTest(unittest.TestCase):
    """The live bridge consumes a growing bytearray in place."""

    def test_consumes_only_complete_frames(self):
        buffer = bytearray(pen2d_frame(1, 2, 3, 4, 0))
        partial = pen2d_frame(5, 6, 7, 8, 1)[:5]
        buffer.extend(partial)
        frames = list(repaper_uinput.iter_frames_from_buffer(buffer))
        self.assertEqual(len(frames), 1)
        # The partial frame must survive for the next read.
        self.assertEqual(bytes(buffer), partial)

    def test_drops_bytes_with_no_signature(self):
        buffer = bytearray(b'\x00\x01\x02\x03\x04\x05')
        self.assertEqual(list(repaper_uinput.iter_frames_from_buffer(buffer)), [])
        # Up to two bytes are retained so a signature split across two reads
        # is not lost; everything before that is discarded.
        self.assertLessEqual(len(buffer), 2)

    def test_signature_split_across_reads_is_recovered(self):
        built = pen2d_frame(11, 22, 33, 44, 1)
        buffer = bytearray(built[:2])          # first read ends mid-signature
        self.assertEqual(list(repaper_uinput.iter_frames_from_buffer(buffer)), [])
        buffer.extend(built[2:])               # remainder arrives next read
        frames = list(repaper_uinput.iter_frames_from_buffer(buffer))
        self.assertEqual(len(frames), 1)
        self.assertEqual(repaper_uinput.parse_pen(frames[0])['x'], 11)


class PenPresenceTest(unittest.TestCase):
    """The tablet streams whether or not a pen is over it.

    Real samples carry an orientation vector inside the unit circle; the
    idle noise does not, which is what separates them.
    """

    def sample(self, rot_x, rot_y):
        return {'rot_x': rot_x, 'rot_y': rot_y}

    def test_accepts_measured_hover_vectors(self):
        for rot_x, rot_y in ((1118, -9101), (4147, -7725), (2779, -8786)):
            self.assertTrue(repaper_uinput.pen_present(self.sample(rot_x, rot_y)),
                            f'({rot_x}, {rot_y}) should be a valid pen vector')

    def test_rejects_measured_idle_noise(self):
        # Captured with no pen on the tablet: both components near full scale,
        # which puts the vector well outside the unit circle.
        for rot_x, rot_y in ((9007, 9054), (9134, 9122), (9100, 9100)):
            self.assertFalse(repaper_uinput.pen_present(self.sample(rot_x, rot_y)),
                             f'({rot_x}, {rot_y}) should be rejected as noise')

    def test_accepts_a_perfectly_vertical_pen(self):
        self.assertTrue(repaper_uinput.pen_present(self.sample(0, 0)))

    def test_accepts_full_tilt_on_one_axis(self):
        self.assertTrue(repaper_uinput.pen_present(self.sample(10000, 0)))

    def test_accepts_steep_tilt_with_both_components_large(self):
        # Measured mid-stroke: tilt_y saturated at -90 while tilt_x was
        # around 25 degrees, giving a magnitude near 11200.  A tighter
        # cutoff rejected these and broke the stroke.
        for rot_x, rot_y in ((5150, -10000), (4226, -10000), (2250, -10000)):
            self.assertTrue(repaper_uinput.pen_present(self.sample(rot_x, rot_y)),
                            f'({rot_x}, {rot_y}) is a real steep-tilt sample')

    def test_cutoff_sits_between_steep_tilt_and_noise(self):
        steep = 5150 ** 2 + 10000 ** 2
        noise = 9007 ** 2 + 9054 ** 2
        limit = (repaper_uinput.ROT_UNIT * repaper_uinput.ROT_TOLERANCE) ** 2
        self.assertLess(steep, limit, 'real steep-tilt samples must pass')
        self.assertGreater(noise, limit, 'idle noise must still be rejected')


class TiltTest(unittest.TestCase):
    """rot components are a unit vector scaled by ROT_UNIT, so tilt is asin."""

    def test_vertical_pen_is_zero_degrees(self):
        self.assertEqual(repaper_uinput.tilt_degrees(0), 0)

    def test_full_scale_is_ninety_degrees(self):
        self.assertEqual(repaper_uinput.tilt_degrees(repaper_uinput.ROT_UNIT), 90)
        self.assertEqual(repaper_uinput.tilt_degrees(-repaper_uinput.ROT_UNIT), -90)

    def test_half_scale_is_thirty_degrees(self):
        # asin(0.5) is 30 degrees; a linear scaling would wrongly give 45.
        self.assertEqual(repaper_uinput.tilt_degrees(repaper_uinput.ROT_UNIT // 2),
                         30)

    def test_is_symmetric(self):
        for value in (1000, 3000, 7000):
            self.assertEqual(repaper_uinput.tilt_degrees(value),
                             -repaper_uinput.tilt_degrees(-value))

    def test_stays_within_kernel_limits(self):
        for value in (-30000, -10001, 10001, 30000):
            self.assertLessEqual(abs(repaper_uinput.tilt_degrees(value)), 90)


class ButtonTest(unittest.TestCase):
    """Block 0x08 carries the five case buttons as one byte.

    Press codes run from BUTTON_PRESS_BASE, and the release code for a
    button is its press code plus BUTTON_RELEASE_OFFSET.
    """

    def test_press_codes_map_to_indices(self):
        for index, code in enumerate(range(0x0a, 0x0f)):
            self.assertEqual(decode_stream.parse_button(bytes([code])),
                             (index, True))

    def test_release_codes_map_to_the_same_indices(self):
        for index, code in enumerate(range(0x0f, 0x14)):
            self.assertEqual(decode_stream.parse_button(bytes([code])),
                             (index, False))

    def test_release_is_press_plus_the_offset(self):
        for index in range(decode_stream.BUTTON_COUNT):
            press = decode_stream.BUTTON_PRESS_BASE + index
            release = press + decode_stream.BUTTON_RELEASE_OFFSET
            self.assertEqual(decode_stream.parse_button(bytes([press])),
                             (index, True))
            self.assertEqual(decode_stream.parse_button(bytes([release])),
                             (index, False))

    def test_rejects_codes_outside_the_range(self):
        for code in (0x00, 0x09, 0x14, 0xff):
            self.assertIsNone(decode_stream.parse_button(bytes([code])))

    def test_rejects_a_wrong_length_payload(self):
        self.assertIsNone(decode_stream.parse_button(b''))
        self.assertIsNone(decode_stream.parse_button(b'\x0a\x0a'))

    def test_block_size_matches_a_single_byte(self):
        self.assertEqual(
            decode_stream.PAYLOAD_SIZES[decode_stream.BUTTON_BLOCK], 1)

    def test_observed_transition_sequence_decodes(self):
        # Captured while pressing buttons: each press is followed by the
        # matching release, five apart.
        observed = [0x0c, 0x11, 0x0e, 0x13, 0x0d, 0x12, 0x0b, 0x10]
        decoded = [decode_stream.parse_button(bytes([c])) for c in observed]
        self.assertEqual(decoded, [(2, True), (2, False), (4, True), (4, False),
                                   (3, True), (3, False), (1, True), (1, False)])


class OrientationTest(unittest.TestCase):

    class Args:
        x_min, x_max = -100, 100
        y_min, y_max = -200, 200
        orientation = 'portrait'

    def transform(self, orientation, x, y):
        args = self.Args()
        args.orientation = orientation
        return repaper_uinput.transform_sample({'x': x, 'y': y}, args)

    def test_portrait_is_identity(self):
        self.assertEqual(self.transform('portrait', 50, -75), (50, -75))

    def test_landscape_aliases_clockwise(self):
        self.assertEqual(self.transform('landscape', 50, -75),
                         self.transform('landscape-cw', 50, -75))

    def test_rotations_are_inverses(self):
        cw_x, cw_y = self.transform('landscape-cw', 50, -75)
        # Rotating back the other way must recover the original point.
        self.assertEqual(self.transform('landscape-ccw', cw_x, cw_y), (50, -75))

    def test_corners_stay_inside_the_rotated_range(self):
        for x in (-100, 100):
            for y in (-200, 200):
                out_x, out_y = self.transform('landscape-cw', x, y)
                self.assertGreaterEqual(out_x, -200)
                self.assertLessEqual(out_x, 200)
                self.assertGreaterEqual(out_y, -100)
                self.assertLessEqual(out_y, 100)

    def test_clamps_out_of_range_input(self):
        self.assertEqual(self.transform('portrait', 9999, -9999), (100, -200))

    def test_rejects_unknown_orientation(self):
        with self.assertRaises(ValueError):
            self.transform('upside-down', 0, 0)


class AxisBoundsTest(unittest.TestCase):

    def test_landscape_swaps_the_axis_ranges(self):
        args = OrientationTest.Args()
        args.orientation = 'landscape-cw'
        self.assertEqual(repaper_uinput.axis_bounds(args), (-200, 200, -100, 100))

    def test_portrait_keeps_the_axis_ranges(self):
        args = OrientationTest.Args()
        args.orientation = 'portrait'
        self.assertEqual(repaper_uinput.axis_bounds(args), (-100, 100, -200, 200))


if __name__ == '__main__':
    unittest.main()
