// SPDX-License-Identifier: GPL-2.0-only
/*
 * HID driver for ISKN Repaper paper tablet
 *
 * The device exposes a standard HID digitizer (report ID 2) alongside a
 * relative mouse (report ID 1).  hid-generic binds and parses the descriptor
 * correctly but marks the digitizer with INPUT_PROP_POINTER instead of
 * INPUT_PROP_DIRECT, so drawing applications (Krita, GIMP, etc.) do not
 * recognise it as a tablet.  This driver fixes that and takes ownership of
 * the device so that hid-generic does not race with it on future plug-ins.
 */

#include <linux/hid.h>
#include <linux/input.h>
#include <linux/module.h>

#define USB_VENDOR_ID_ISKN          0x2c87
#define USB_DEVICE_ID_ISKN_REPAPER  0x0001

/*
 * Called after the input device has been fully configured from the HID
 * report descriptor but before it is registered with the input subsystem.
 * We fix the properties only on the digitizer input (identifiable by the
 * presence of ABS_PRESSURE, which the mouse sub-device does not have).
 */
static int iskn_input_configured(struct hid_device *hdev,
				  struct hid_input *hidinput)
{
	struct input_dev *input = hidinput->input;

	if (!test_bit(ABS_PRESSURE, input->absbit))
		return 0;

	/* Tablet coordinates map directly to the surface, not via a cursor. */
	__set_bit(INPUT_PROP_DIRECT, input->propbit);
	__clear_bit(INPUT_PROP_POINTER, input->propbit);

	hid_info(hdev, "pen digitizer configured (direct input)\n");
	return 0;
}

static int iskn_probe(struct hid_device *hdev, const struct hid_device_id *id)
{
	int ret;

	ret = hid_parse(hdev);
	if (ret) {
		hid_err(hdev, "hid_parse failed: %d\n", ret);
		return ret;
	}

	ret = hid_hw_start(hdev, HID_CONNECT_DEFAULT);
	if (ret)
		hid_err(hdev, "hid_hw_start failed: %d\n", ret);

	return ret;
}

static const struct hid_device_id iskn_devices[] = {
	{ HID_USB_DEVICE(USB_VENDOR_ID_ISKN, USB_DEVICE_ID_ISKN_REPAPER) },
	{ }
};
MODULE_DEVICE_TABLE(hid, iskn_devices);

static struct hid_driver iskn_driver = {
	.name             = "hid-iskn",
	.id_table         = iskn_devices,
	.probe            = iskn_probe,
	.input_configured = iskn_input_configured,
};
module_hid_driver(iskn_driver);

MODULE_AUTHOR("repaper_drv");
MODULE_DESCRIPTION("ISKN Repaper tablet driver");
MODULE_LICENSE("GPL");
