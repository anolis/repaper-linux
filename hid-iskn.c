// SPDX-License-Identifier: GPL-2.0-only
/*
 * HID driver for the ISKN Repaper / Slate paper tablet
 *
 * The tablet tracks a magnet ring on an ordinary pen.  Its HID report
 * descriptor advertises a full digitizer on report ID 2 -- tip switch,
 * eraser, tilt and a 0..4095 pressure field -- but that report never fires,
 * so hid-generic produces an input device that stays permanently silent.
 *
 * The pen data travels over a vendor protocol instead.  That protocol is
 * usually described as being serial-only, but it is equally available over
 * HID: requests go out as output report 4 and replies arrive as input
 * report 3, in identical framing.  This driver therefore needs neither the
 * CDC-ACM interface nor a userspace daemon.
 *
 * Frames look like:
 *
 *	b3 a5 e1 <block> <payload> <crc16-le>
 *
 * with a CRC-16/XMODEM over the payload alone.  Subscribing (block 0x33)
 * takes a 16-bit bitmask rather than a stream id: bit N enables the
 * auto-block whose type is 0x02 + N.
 */

#include <linux/bitops.h>
#include <linux/hid.h>
#include <linux/input.h>
#include <linux/jiffies.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include <linux/unaligned.h>

#define USB_VENDOR_ID_ISKN		0x2c87
#define USB_DEVICE_ID_ISKN_REPAPER	0x0001

#define ISKN_REPORT_ID_IN		0x03
#define ISKN_REPORT_ID_OUT		0x04
#define ISKN_REPORT_PAYLOAD		63

#define ISKN_BLOCK_SUBSCRIBE		0x33
#define ISKN_BLOCK_PEN3D		0x05
#define ISKN_BLOCK_BUTTONS		0x08

/* Bit N of the subscribe mask enables the auto-block of type 0x02 + N. */
#define ISKN_AUTO_BLOCK_BASE		0x02
#define ISKN_AUTO_MASK(block)		BIT((block) - ISKN_AUTO_BLOCK_BASE)
#define ISKN_SUBSCRIPTION		(ISKN_AUTO_MASK(ISKN_BLOCK_PEN3D) | \
					 ISKN_AUTO_MASK(ISKN_BLOCK_BUTTONS))

#define ISKN_PEN3D_FRAME_LEN		19
#define ISKN_PEN3D_PAYLOAD_LEN		13
#define ISKN_BUTTON_FRAME_LEN		7
#define ISKN_BUTTON_PAYLOAD_LEN		1
#define ISKN_FRAME_OVERHEAD		6	/* signature + block + crc */

/*
 * The five case buttons arrive as one byte in block 0x08: a code per button
 * for press, and the same code plus ISKN_BUTTON_RELEASE_OFFSET for release.
 */
#define ISKN_BUTTON_COUNT		5
#define ISKN_BUTTON_PRESS_BASE		0x0a
#define ISKN_BUTTON_RELEASE_OFFSET	5

/*
 * rot_x and rot_y are the x/y components of the pen's unit orientation
 * vector scaled by ISKN_ROT_UNIT.  Real samples stay inside the unit
 * circle; with no pen over the surface the tablet keeps streaming and emits
 * vectors well outside it, so the magnitude doubles as a proximity test.
 */
#define ISKN_ROT_UNIT			10000
/*
 * Held pens reach a magnitude around 11200 at steep tilt, while the idle
 * noise sits near 12800, so the cutoff has to sit between the two.  Too
 * tight and real samples are dropped mid-stroke; too loose and the noise
 * becomes cursor motion.
 */
#define ISKN_ROT_LIMIT_SQ		((s64)12000 * 12000)

/* z pins to this while in contact and runs negative while hovering. */
#define ISKN_Z_CONTACT			300
#define ISKN_Z_MIN			-5200

#define ISKN_TILT_LIMIT			90

/*
 * Coordinate bounds measured from a full-surface sweep.  At the scale
 * below they describe a 158 x 215 mm area, consistent with the A5 sheet
 * the tablet is built around.  Surfaces vary between units, so these stay
 * module parameters rather than fixed constants.
 */
#define ISKN_X_MIN_DEFAULT		-7858
#define ISKN_X_MAX_DEFAULT		7916
#define ISKN_Y_MIN_DEFAULT		-10458
#define ISKN_Y_MAX_DEFAULT		10995

/* Raw units per millimetre: the vendor API scales raw values by 0.01. */
#define ISKN_RESOLUTION			100

/* Contact is binary, but applications expect a pressure axis to exist. */
#define ISKN_PRESSURE_MAX		1024

#define ISKN_PROXIMITY_MS		120

static int x_min = ISKN_X_MIN_DEFAULT;
static int x_max = ISKN_X_MAX_DEFAULT;
static int y_min = ISKN_Y_MIN_DEFAULT;
static int y_max = ISKN_Y_MAX_DEFAULT;

/*
 * The protocol's axes do not match how the surface is held: x and y are
 * transposed, and y then runs the wrong way, so reporting the fields
 * directly moves the cursor sideways and upside down.  These defaults
 * describe the tablet in its normal orientation; the parameters exist for
 * anyone holding it differently.
 */
static bool swap_xy = true;
static bool invert_x;
static bool invert_y = true;

module_param(x_min, int, 0444);
MODULE_PARM_DESC(x_min, "minimum raw X coordinate");
module_param(x_max, int, 0444);
MODULE_PARM_DESC(x_max, "maximum raw X coordinate");
module_param(y_min, int, 0444);
MODULE_PARM_DESC(y_min, "minimum raw Y coordinate");
module_param(y_max, int, 0444);
MODULE_PARM_DESC(y_max, "maximum raw Y coordinate");
module_param(swap_xy, bool, 0444);
MODULE_PARM_DESC(swap_xy, "exchange the X and Y axes (default on)");
module_param(invert_x, bool, 0444);
MODULE_PARM_DESC(invert_x, "mirror the reported X axis");
module_param(invert_y, bool, 0444);
MODULE_PARM_DESC(invert_y, "mirror the reported Y axis (default on)");

/*
 * Bounds of the axes as reported.  After a swap the reported X axis is
 * driven by the raw Y field, so it must advertise and clamp to the raw Y
 * range.
 */
static int iskn_out_x_min(void) { return swap_xy ? y_min : x_min; }
static int iskn_out_x_max(void) { return swap_xy ? y_max : x_max; }
static int iskn_out_y_min(void) { return swap_xy ? x_min : y_min; }
static int iskn_out_y_max(void) { return swap_xy ? x_max : y_max; }

static const u8 iskn_signature[3] = { 0xb3, 0xa5, 0xe1 };

struct iskn_drvdata {
	struct hid_device *hdev;
	struct input_dev *input;
	struct input_dev *pad;
	struct delayed_work proximity_work;
	bool in_proximity;
};

/* Pad buttons are exposed the way tablet drivers conventionally do. */
static const unsigned short iskn_pad_keys[ISKN_BUTTON_COUNT] = {
	BTN_0, BTN_1, BTN_2, BTN_3, BTN_4,
};

/*
 * CRC-16/XMODEM: CCITT polynomial, init 0, not reflected.  The kernel's
 * crc_ccitt() is the reflected variant and crc_ccitt_false() is not
 * available everywhere, so keep the ten lines here.
 */
static u16 iskn_crc16(const u8 *data, size_t len)
{
	u16 crc = 0;
	size_t i;

	for (i = 0; i < len; i++) {
		u8 byte = data[i] ^ ((crc >> 8) & 0xff);

		byte ^= byte >> 4;
		crc = (crc << 8) ^ ((u16)byte << 12) ^ ((u16)byte << 5) ^ byte;
	}
	return crc;
}

/* sin(d) * 10000 for d = 0..90, used to invert the orientation vector. */
static const u16 iskn_sin_table[91] = {
	0, 175, 349, 523, 698, 872, 1045, 1219, 1392, 1564,
	1736, 1908, 2079, 2250, 2419, 2588, 2756, 2924, 3090, 3256,
	3420, 3584, 3746, 3907, 4067, 4226, 4384, 4540, 4695, 4848,
	5000, 5150, 5299, 5446, 5592, 5736, 5878, 6018, 6157, 6293,
	6428, 6561, 6691, 6820, 6947, 7071, 7193, 7314, 7431, 7547,
	7660, 7771, 7880, 7986, 8090, 8192, 8290, 8387, 8480, 8572,
	8660, 8746, 8829, 8910, 8988, 9063, 9135, 9205, 9272, 9336,
	9397, 9455, 9511, 9563, 9613, 9659, 9703, 9744, 9781, 9816,
	9848, 9877, 9903, 9925, 9945, 9962, 9976, 9986, 9994, 9998,
	10000,
};

/* Integer asin: component of a unit vector scaled by 10000 -> degrees. */
static int iskn_tilt_degrees(s16 component)
{
	int sign = component < 0 ? -1 : 1;
	int magnitude = abs(component);
	int low = 0, high = ISKN_TILT_LIMIT;

	if (magnitude >= ISKN_ROT_UNIT)
		return sign * ISKN_TILT_LIMIT;

	while (low < high) {
		int mid = (low + high) / 2;

		if (iskn_sin_table[mid] < magnitude)
			low = mid + 1;
		else
			high = mid;
	}
	return sign * low;
}

/*
 * The tablet streams whether or not a pen is over it.  Reject the noise it
 * emits with nothing to track, which would otherwise become cursor motion.
 */
static bool iskn_pen_present(s16 rot_x, s16 rot_y)
{
	s64 magnitude_sq = (s64)rot_x * rot_x + (s64)rot_y * rot_y;

	return magnitude_sq <= ISKN_ROT_LIMIT_SQ;
}

static void iskn_report_out_of_proximity(struct iskn_drvdata *drvdata)
{
	struct input_dev *input = drvdata->input;

	input_report_key(input, BTN_TOUCH, 0);
	input_report_key(input, BTN_TOOL_PEN, 0);
	input_report_abs(input, ABS_PRESSURE, 0);
	input_sync(input);
	drvdata->in_proximity = false;
}

/*
 * Without this the pen never leaves proximity and applications hold a stale
 * cursor forever once it is lifted away from the surface.
 */
static void iskn_proximity_expired(struct work_struct *work)
{
	struct iskn_drvdata *drvdata = container_of(to_delayed_work(work),
						    struct iskn_drvdata,
						    proximity_work);

	if (drvdata->in_proximity)
		iskn_report_out_of_proximity(drvdata);
}

static void iskn_report_pen3d(struct iskn_drvdata *drvdata, const u8 *payload)
{
	struct input_dev *input = drvdata->input;
	s16 x = get_unaligned_le16(payload);
	s16 y = get_unaligned_le16(payload + 2);
	s16 z = get_unaligned_le16(payload + 4);
	/* payload + 6 is a frame counter, not a coordinate. */
	s16 rot_x = get_unaligned_le16(payload + 8);
	s16 rot_y = get_unaligned_le16(payload + 10);
	bool touch = payload[12] != 0;
	int out_x, out_y, tilt_x, tilt_y;

	if (!iskn_pen_present(rot_x, rot_y))
		return;

	/* Tilt travels with its axis, so swap the vector alongside it. */
	if (swap_xy) {
		out_x = y;
		out_y = x;
		tilt_x = iskn_tilt_degrees(rot_y);
		tilt_y = iskn_tilt_degrees(rot_x);
	} else {
		out_x = x;
		out_y = y;
		tilt_x = iskn_tilt_degrees(rot_x);
		tilt_y = iskn_tilt_degrees(rot_y);
	}

	if (invert_x) {
		out_x = iskn_out_x_min() + iskn_out_x_max() - out_x;
		tilt_x = -tilt_x;
	}
	if (invert_y) {
		out_y = iskn_out_y_min() + iskn_out_y_max() - out_y;
		tilt_y = -tilt_y;
	}

	input_report_key(input, BTN_TOOL_PEN, 1);
	input_report_key(input, BTN_TOUCH, touch);
	input_report_abs(input, ABS_X,
			 clamp_val(out_x, iskn_out_x_min(), iskn_out_x_max()));
	input_report_abs(input, ABS_Y,
			 clamp_val(out_y, iskn_out_y_min(), iskn_out_y_max()));
	input_report_abs(input, ABS_PRESSURE, touch ? ISKN_PRESSURE_MAX : 0);
	input_report_abs(input, ABS_DISTANCE, clamp_val(z, ISKN_Z_MIN,
							ISKN_Z_CONTACT));
	input_report_abs(input, ABS_TILT_X, tilt_x);
	input_report_abs(input, ABS_TILT_Y, tilt_y);
	input_sync(input);

	drvdata->in_proximity = true;
	mod_delayed_work(system_wq, &drvdata->proximity_work,
			 msecs_to_jiffies(ISKN_PROXIMITY_MS));
}

static void iskn_report_button(struct iskn_drvdata *drvdata, u8 code)
{
	int index = code - ISKN_BUTTON_PRESS_BASE;
	bool pressed = index < ISKN_BUTTON_RELEASE_OFFSET;

	if (!pressed)
		index -= ISKN_BUTTON_RELEASE_OFFSET;
	if (index < 0 || index >= ISKN_BUTTON_COUNT || !drvdata->pad)
		return;

	input_report_key(drvdata->pad, iskn_pad_keys[index], pressed);
	input_sync(drvdata->pad);
}


/*
 * One HID report can carry several frames, and the tail is zero padded.
 * Walk by signature so a partial or unknown block cannot swallow whatever
 * follows it.
 */
static void iskn_parse_report(struct iskn_drvdata *drvdata, const u8 *data,
			      int size)
{
	int offset = 0;

	while (offset + ISKN_FRAME_OVERHEAD <= size) {
		const u8 *frame = data + offset;
		const u8 *payload;
		u16 expected, actual;

		if (memcmp(frame, iskn_signature, sizeof(iskn_signature))) {
			offset++;
			continue;
		}

		if (frame[3] == ISKN_BLOCK_PEN3D &&
		    offset + ISKN_PEN3D_FRAME_LEN <= size) {
			payload = frame + 4;
			expected = get_unaligned_le16(payload +
						      ISKN_PEN3D_PAYLOAD_LEN);
			actual = iskn_crc16(payload, ISKN_PEN3D_PAYLOAD_LEN);
			if (expected == actual)
				iskn_report_pen3d(drvdata, payload);
			offset += ISKN_PEN3D_FRAME_LEN;
			continue;
		}

		if (frame[3] == ISKN_BLOCK_BUTTONS &&
		    offset + ISKN_BUTTON_FRAME_LEN <= size) {
			payload = frame + 4;
			expected = get_unaligned_le16(payload +
						      ISKN_BUTTON_PAYLOAD_LEN);
			actual = iskn_crc16(payload, ISKN_BUTTON_PAYLOAD_LEN);
			if (expected == actual)
				iskn_report_button(drvdata, payload[0]);
			offset += ISKN_BUTTON_FRAME_LEN;
			continue;
		}

		offset += sizeof(iskn_signature);
	}
}

static int iskn_raw_event(struct hid_device *hdev, struct hid_report *report,
			  u8 *data, int size)
{
	struct iskn_drvdata *drvdata = hid_get_drvdata(hdev);

	if (!drvdata || !drvdata->input || size < 2)
		return 0;

	if (data[0] != ISKN_REPORT_ID_IN)
		return 0;

	iskn_parse_report(drvdata, data + 1, size - 1);
	return 1;
}

/* Send one framed vendor packet as an output report. */
static int iskn_send_block(struct hid_device *hdev, u8 block,
			   const u8 *payload, size_t payload_len)
{
	size_t frame_len = sizeof(iskn_signature) + 1 + payload_len + 2;
	u8 *buf;
	u16 crc;
	int ret;

	if (frame_len > ISKN_REPORT_PAYLOAD)
		return -EINVAL;

	buf = kzalloc(ISKN_REPORT_PAYLOAD + 1, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	buf[0] = ISKN_REPORT_ID_OUT;
	memcpy(buf + 1, iskn_signature, sizeof(iskn_signature));
	buf[4] = block;
	memcpy(buf + 5, payload, payload_len);
	crc = iskn_crc16(payload, payload_len);
	put_unaligned_le16(crc, buf + 5 + payload_len);

	ret = hid_hw_output_report(hdev, buf, ISKN_REPORT_PAYLOAD + 1);
	kfree(buf);
	return ret < 0 ? ret : 0;
}

static int iskn_subscribe(struct hid_device *hdev, u16 mask)
{
	u8 payload[2];

	put_unaligned_le16(mask, payload);
	return iskn_send_block(hdev, ISKN_BLOCK_SUBSCRIBE, payload,
			       sizeof(payload));
}

static int iskn_input_open(struct input_dev *input)
{
	struct iskn_drvdata *drvdata = input_get_drvdata(input);

	return hid_hw_open(drvdata->hdev);
}

static void iskn_input_close(struct input_dev *input)
{
	struct iskn_drvdata *drvdata = input_get_drvdata(input);

	hid_hw_close(drvdata->hdev);
}

static int iskn_register_input(struct iskn_drvdata *drvdata)
{
	struct hid_device *hdev = drvdata->hdev;
	struct input_dev *input;

	input = devm_input_allocate_device(&hdev->dev);
	if (!input)
		return -ENOMEM;

	input->name = "ISKN Repaper Pen";
	input->phys = hdev->phys;
	input->uniq = hdev->uniq;
	input->id.bustype = BUS_USB;
	input->id.vendor = hdev->vendor;
	input->id.product = hdev->product;
	input->id.version = hdev->version;
	input->dev.parent = &hdev->dev;
	input->open = iskn_input_open;
	input->close = iskn_input_close;

	input_set_drvdata(input, drvdata);

	/*
	 * An external tablet is a pointer device.  INPUT_PROP_DIRECT means
	 * display-integrated and would mislead userspace about screen
	 * mapping.
	 */
	__set_bit(INPUT_PROP_POINTER, input->propbit);

	__set_bit(EV_KEY, input->evbit);
	__set_bit(EV_ABS, input->evbit);
	__set_bit(BTN_TOOL_PEN, input->keybit);
	__set_bit(BTN_TOUCH, input->keybit);

	input_set_abs_params(input, ABS_X, iskn_out_x_min(), iskn_out_x_max(),
			     0, 0);
	input_set_abs_params(input, ABS_Y, iskn_out_y_min(), iskn_out_y_max(),
			     0, 0);
	input_abs_set_res(input, ABS_X, ISKN_RESOLUTION);
	input_abs_set_res(input, ABS_Y, ISKN_RESOLUTION);

	input_set_abs_params(input, ABS_PRESSURE, 0, ISKN_PRESSURE_MAX, 0, 0);
	input_set_abs_params(input, ABS_DISTANCE, ISKN_Z_MIN, ISKN_Z_CONTACT,
			     0, 0);
	input_set_abs_params(input, ABS_TILT_X, -ISKN_TILT_LIMIT,
			     ISKN_TILT_LIMIT, 0, 0);
	input_set_abs_params(input, ABS_TILT_Y, -ISKN_TILT_LIMIT,
			     ISKN_TILT_LIMIT, 0, 0);

	drvdata->input = input;
	return input_register_device(input);
}

/*
 * The case buttons go on their own input device rather than onto the pen.
 * That is how tablet pads are conventionally exposed, and it keeps the pen
 * device advertising only what a stylus actually has.
 */
static int iskn_register_pad(struct iskn_drvdata *drvdata)
{
	struct hid_device *hdev = drvdata->hdev;
	struct input_dev *pad;
	int i;

	pad = devm_input_allocate_device(&hdev->dev);
	if (!pad)
		return -ENOMEM;

	pad->name = "ISKN Repaper Pad";
	pad->phys = hdev->phys;
	pad->uniq = hdev->uniq;
	pad->id.bustype = BUS_USB;
	pad->id.vendor = hdev->vendor;
	pad->id.product = hdev->product;
	pad->id.version = hdev->version;
	pad->dev.parent = &hdev->dev;
	pad->open = iskn_input_open;
	pad->close = iskn_input_close;

	input_set_drvdata(pad, drvdata);

	__set_bit(EV_KEY, pad->evbit);
	for (i = 0; i < ISKN_BUTTON_COUNT; i++)
		__set_bit(iskn_pad_keys[i], pad->keybit);

	drvdata->pad = pad;
	return input_register_device(pad);
}

static int iskn_probe(struct hid_device *hdev, const struct hid_device_id *id)
{
	struct iskn_drvdata *drvdata;
	int ret;

	drvdata = devm_kzalloc(&hdev->dev, sizeof(*drvdata), GFP_KERNEL);
	if (!drvdata)
		return -ENOMEM;

	drvdata->hdev = hdev;
	INIT_DELAYED_WORK(&drvdata->proximity_work, iskn_proximity_expired);
	hid_set_drvdata(hdev, drvdata);

	ret = hid_parse(hdev);
	if (ret) {
		hid_err(hdev, "hid_parse failed: %d\n", ret);
		return ret;
	}

	/*
	 * Deliberately do not connect HID_CONNECT_HIDINPUT: the descriptor's
	 * digitizer report never fires, so letting it build an input device
	 * only produces a silent one that applications try to use.
	 */
	ret = hid_hw_start(hdev, HID_CONNECT_HIDRAW | HID_CONNECT_DRIVER);
	if (ret) {
		hid_err(hdev, "hid_hw_start failed: %d\n", ret);
		return ret;
	}

	ret = iskn_register_input(drvdata);
	if (ret) {
		hid_err(hdev, "input registration failed: %d\n", ret);
		goto err_stop;
	}

	ret = iskn_register_pad(drvdata);
	if (ret) {
		hid_err(hdev, "pad registration failed: %d\n", ret);
		goto err_stop;
	}

	/* Reports only arrive while the transport is open. */
	ret = hid_hw_open(hdev);
	if (ret) {
		hid_err(hdev, "hid_hw_open failed: %d\n", ret);
		goto err_stop;
	}

	ret = iskn_subscribe(hdev, ISKN_SUBSCRIPTION);
	if (ret) {
		hid_err(hdev, "subscribe failed: %d\n", ret);
		goto err_close;
	}

	hid_info(hdev, "pen and buttons subscribed over HID (mask 0x%04lx)\n",
		 ISKN_SUBSCRIPTION);
	return 0;

err_close:
	hid_hw_close(hdev);
err_stop:
	hid_hw_stop(hdev);
	return ret;
}

static void iskn_remove(struct hid_device *hdev)
{
	struct iskn_drvdata *drvdata = hid_get_drvdata(hdev);

	if (drvdata) {
		cancel_delayed_work_sync(&drvdata->proximity_work);
		iskn_subscribe(hdev, 0);
	}
	hid_hw_close(hdev);
	hid_hw_stop(hdev);
}

#ifdef CONFIG_PM
static int iskn_resume(struct hid_device *hdev)
{
	/* The subscription does not survive a suspend. */
	return iskn_subscribe(hdev, ISKN_SUBSCRIPTION);
}
#endif

static const struct hid_device_id iskn_devices[] = {
	{ HID_USB_DEVICE(USB_VENDOR_ID_ISKN, USB_DEVICE_ID_ISKN_REPAPER) },
	{ }
};
MODULE_DEVICE_TABLE(hid, iskn_devices);

static struct hid_driver iskn_driver = {
	.name		= "hid-iskn",
	.id_table	= iskn_devices,
	.probe		= iskn_probe,
	.remove		= iskn_remove,
	.raw_event	= iskn_raw_event,
#ifdef CONFIG_PM
	.resume		= iskn_resume,
#endif
};
module_hid_driver(iskn_driver);

MODULE_AUTHOR("repaper-linux");
MODULE_DESCRIPTION("ISKN Repaper paper tablet driver");
MODULE_LICENSE("GPL");
