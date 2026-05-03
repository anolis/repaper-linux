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

## Trace the vendor library

If the Python probes are silent, build the vendor-library harness and syscall
tracer:

```sh
make tools
```

Run the harness under the tracer:

```sh
sudo env LD_PRELOAD="$PWD/trace_serial.so" ./iskn_harness 32
```

The tracer prints serial `open`, `ioctl`, `write`, and `read` activity. This is
useful for capturing the exact packets sent by `libISKN_API.so.1.0.0` without
vendor headers.

Captured trace or probe output can be piped into the decoder while the packet
layout is being mapped:

```sh
sudo env LD_PRELOAD="$PWD/trace_serial.so" ./iskn_harness 32 2>&1 | tee trace.log
python3 ./decode_stream.py < trace.log
```

The live stream observed so far includes `0x04` packets with a 9-byte payload
and `0x18` packets with a 14-byte payload, depending on the subscription path.

## Current status

- Kernel module: claims `2c87:0001` and fixes tablet input properties.
- Probe tool: listens for raw HID/serial traffic and tries a few exploratory
  output reports. Its ISKN serial packets use the vendor framing and payload
  CRC format.
- Evdev probe: verifies whether Linux input events are being emitted for the
  tablet surface.
- Vendor harness: calls `libISKN_API.so.1.0.0` directly and can be run under
  `trace_serial.so` to capture the real serial protocol.
- Stream decoder: extracts framed serial packets and prints raw 16-bit fields
  while the pen report layout is being mapped.
- Vendor library: `libISKN_API.so.1.0.0` is present for possible future
  reverse-engineering, but the kernel module does not use it.
