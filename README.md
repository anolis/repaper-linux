# repaper_drv

Linux HID driver experiment for the ISKN Repaper tablet (`2c87:0001`).

The Repaper already exposes a HID digitizer report, but `hid-generic` can
classify the pen input as a pointer device. This module claims the tablet and
marks the pen input as `INPUT_PROP_DIRECT`, which makes applications treat it
as a tablet surface instead of a relative pointer.

## Build

Install matching kernel headers, then run:

```sh
make
```

## Load for testing

```sh
sudo insmod ./hid-iskn.ko
```

Unplug and replug the tablet if it was already bound to `hid-generic`.

Check the bound driver:

```sh
cat /sys/bus/hid/devices/0003:2C87:0001.*/uevent
```

Expected output includes:

```text
DRIVER=hid-iskn
HID_NAME=ISKN Repaper
```

Check the input property:

```sh
cat /sys/bus/hid/devices/0003:2C87:0001.*/input/input*/properties
```

The pen input should report `2`, which is `INPUT_PROP_DIRECT`.

## Probe raw traffic

`probe.py` discovers the Repaper hidraw node from sysfs and optionally looks
for a matching `ttyACM` serial interface.

```sh
sudo ./probe.py
```

Manual overrides are available when needed:

```sh
sudo ./probe.py --hidraw /dev/hidraw9 --serial /dev/ttyACM0
```

If HID/serial probing prints no responses, test the normal Linux input path.
This is the path drawing applications use:

```sh
sudo ./evprobe.py
```

Move the pen and press on the surface while it listens. The output should show
`EV_ABS` events such as `ABS_X`, `ABS_Y`, and `ABS_PRESSURE`, plus `EV_KEY`
tool/touch events.

## Current status

- Kernel module: claims `2c87:0001` and fixes tablet input properties.
- Probe tool: listens for raw HID/serial traffic and tries a few exploratory
  output reports.
- Evdev probe: verifies whether Linux input events are being emitted for the
  tablet surface.
- Vendor library: `libISKN_API.so.1.0.0` is present for possible future
  reverse-engineering, but the kernel module does not use it.
