# repaper-linux

Linux support for the ISKN Repaper / Slate paper tablet (`2c87:0001`).

The tablet tracks a magnet ring on an ordinary pen, so it reports position,
pen orientation and a contact flag. It exposes three USB interfaces: a HID
interface, and a CDC-ACM serial port. The pen data travels over a vendor
protocol, not over the HID digitizer report.

## Power the tablet on first

**The tablet must be switched on with its own power button.** This is the
single most common reason nothing works.

A powered-off Repaper still enumerates over USB — it appears in `lsusb`,
creates `/dev/ttyACM0` and a hidraw node, and shows a clean enumeration in
`dmesg` with no errors. It simply answers nothing. Every probe returns zero
bytes and HID output reports fail with `ETIMEDOUT`.

Confirm it is actually awake by asking for something that does not involve
the pen:

```sh
python3 ./repaper_uinput.py --calibrate 5
```

A live tablet answers immediately. A sleeping one is silent.

## Quick start

```sh
sudo modprobe uinput
./repaper_uinput.py
```

No `sudo` is needed for the bridge itself: `systemd-logind` grants the
logged-in user access to `/dev/ttyACM*` and `/dev/uinput` via `uaccess`.
Only the raw HID node is root-only; install the bundled rule if you want it:

```sh
sudo cp 99-iskn-repaper.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Calibrate once, then the bounds are remembered in
`~/.config/repaper/calibration.conf`:

```sh
./repaper_uinput.py --calibrate
```

Sweep the pen across the whole surface, touching all four corners and
pressing down for part of the run. Calibration refuses to save unless it
sees enough contact samples, so a run where the pen never touched down
cannot silently produce bad bounds.

Other options:

```sh
./repaper_uinput.py --orientation landscape
./repaper_uinput.py --verbose          # print decoded samples
./repaper_uinput.py --fuzz 8           # damp jitter
./repaper_uinput.py --block pen2d      # older, smaller block
```

The bridge waits for the tablet if it is absent and reconnects after an
unplug. In GIMP, open `Edit` → `Input Devices`, select
`ISKN Repaper Virtual Tablet` and set the mode to `Screen`.

## What the virtual tablet reports

| Axis | Source | Range |
| --- | --- | --- |
| `ABS_X`, `ABS_Y` | pen position | calibrated, resolution 100 units/mm |
| `ABS_DISTANCE` | `z` | hover height |
| `ABS_TILT_X`, `ABS_TILT_Y` | orientation vector | −90…90 degrees |
| `ABS_PRESSURE` | contact flag | 0 or `--pressure` |
| `BTN_TOUCH`, `BTN_TOOL_PEN` | contact, proximity | |

**There is no real pen pressure.** The pen is a passive magnet with no force
sensor. The HID report descriptor advertises a 0–4095 Tip Pressure field,
but that path never reports anything. `ABS_PRESSURE` is therefore a binary
contact indicator, and `z` is a height that saturates on contact rather
than a force.

The device is marked `INPUT_PROP_POINTER`, which is correct for an external
tablet. `INPUT_PROP_DIRECT` means display-integrated (a Cintiq); pass
`--direct` if you have a reason to want it.

## Protocol reference

All confirmed by measurement against the hardware unless marked otherwise.

### Framing

```text
b3 a5 e1 <block-type> <payload> <crc16-le>
```

The checksum is **CRC-16/XMODEM** (CCITT polynomial, init 0) over the
payload only — the block type is not included. Its check value for
`"123456789"` is `0x31C3`.

Frame sizes are fixed per block type; see `FRAME_SIZES` in
`decode_stream.py`.

### Requesting a block once

```text
b3 a5 e1 34 <block-type> <crc16-le>
```

The request id **is** the block type. Known responses:

| Id | Block | Contents |
| --- | --- | --- |
| 1 | `0x01` | status |
| 2 | `0x02` | description, contains `"REPAPER"` |
| 9 | `0x09` | disk status |
| 10 | `0x0a` | file descriptors, one frame per stored file |
| 15 | `0x0f` | unknown |
| 19 | `0x13` | unknown |
| 20 | `0x14` | device name, reads `"Slate X"` |

### Subscribing to streams

```text
b3 a5 e1 33 <mask-le16> <crc16-le>
```

The payload is a **bitmask, not a stream id**. Bit *N* enables the auto-block
whose type is `0x02 + N`, and each subscribe replaces the previous mask.
Sending `0` silences everything.

| Bit | Mask | Block | Rate |
| --- | --- | --- | --- |
| 1 | `0x0002` | `0x03` unknown | ~1 Hz |
| 2 | `0x0004` | `0x04` pen2d | ~96 Hz |
| 3 | `0x0008` | `0x05` pen3d | ~138 Hz |
| 4 | `0x0010` | `0x06` raw3d | ~120 Hz |

This is the bug that historically made the pen look dead: iterating ids
`0..5` and sending them verbatim requests whichever *bits* those integers
happen to set. Use `auto_block_mask()` rather than raw integers.

Bits 12–15 enable block `0x30`, a very high rate stream of three IEEE-754
`float32` values after a 3-byte header — most likely the raw magnetometer
array. Unlike the pen blocks it streams with no pen present.

### Pen payloads

```text
0x04 pen2d  x:i16 y:i16 rot_x:i16 rot_y:i16 state:u8            (9 bytes)
0x05 pen3d  x:i16 y:i16 z:i16 seq:u16 rot_x:i16 rot_y:i16 state:u8  (13 bytes)
0x06 raw3d  x:i16 y:i16 z:i16 rot_x:i16 rot_y:i16              (10 bytes)
```

`pen3d` is a strict superset of `pen2d` and is what the bridge uses.

* **`seq` is a frame counter**, not a coordinate. It advances by exactly 2
  every frame regardless of pen motion (2663 of 2753 consecutive samples in
  one capture; the remainder are dropped frames). Earlier versions of this
  repo read it as a second height called `z_paper`.
* **`z`** pins to `+300` while in contact and runs negative while hovering,
  so it is a height, not a force.
* **`rot_x`, `rot_y`** are the x and y components of the pen's unit
  orientation vector scaled by 10000, so tilt is `asin(rot / 10000)`.
  *Inferred*, but it holds on every real sample measured and it also gives
  a proximity test: real samples satisfy `rot_x² + rot_y² ≤ 10000²`, while
  the noise emitted with no pen present sits far outside the unit circle.
  The bridge uses this to reject idle noise, which otherwise becomes bogus
  cursor motion.

### The same protocol runs over HID

The vendor protocol is available over the HID interface, with no serial
port involved:

* **write** an ISKN packet as HID **output report 4**
* **read** the reply as HID **input report 3**, framed exactly as on serial
  and zero-padded to the report length

```text
03 b3 a5 e1 02 ... 52 45 50 41 50 45 52 00 ... 00
^report id        "REPAPER"
```

This matters because it means a kernel driver needs no CDC-ACM dependency
and no userspace daemon: subscribe with `hid_hw_output_report()` and parse
frames in `raw_event()`.

### The HID digitizer report is not usable

The descriptor declares a full digitizer on report ID 2 — tip switch, in
range, invert, eraser, X/Y tilt, 0–4095 tip pressure, X/Y/Z with physical
units. The kernel parses it and creates an input node advertising all of it.

It never emits a single event, verified with the tablet powered on and
streaming pen data over the vendor protocol at the same time. Treat the
digitizer report as advertised but unimplemented.

## Tools

`repaper_uinput.py` is the bridge and the calibration tool. The rest are for
protocol work:

```sh
./probe.py                 # exploratory HID/serial probing
./evprobe.py               # watch the kernel's own input events
python3 ./decode_stream.py < capture.log      # decode frames from any log
python3 ./decode_stream.py --summary < capture.log
python3 ./decode_stream.py --bounds  < capture.log
```

`decode_stream.py` scrapes hex out of probe and trace logs. It only accepts
whole two-digit tokens appearing in runs, so byte counts in lines like
`[trace] read 15: b3 a5 ...` and stray hex inside `/dev/ttyACM0` are not
mistaken for data.

For the vendor library:

```sh
make tools
sudo env LD_PRELOAD="$PWD/trace_serial.so" ./iskn_harness 32 2>&1 | tee trace.log
python3 ./decode_stream.py < trace.log
```

## Tests

```sh
python3 -m unittest discover -p 'test_*.py'
```

51 tests covering the checksum, framing and resync, payload layouts, the
subscribe bitmask, tilt conversion and the proximity test. Pure stdlib, no
dependencies, and no tablet required.

## Kernel module

`hid-iskn.c` claims `2c87:0001` and marks the digitizer input
`INPUT_PROP_DIRECT`.

```sh
make
sudo insmod ./hid-iskn.ko
```

Note that this only adjusts properties on the digitizer input, and that
input never reports events — so the module currently has no practical
effect. The useful direction is to reimplement it around the vendor HID
pipe described above.

## Status

* **Protocol** — framing, checksum, request table, subscribe bitmask and
  all three pen payloads confirmed against hardware.
* **Bridge** — subscribes correctly, emits position, tilt, hover distance
  and contact, rejects idle noise, reports axis resolution, drops the tool
  out of proximity, and reconnects across unplug.
* **Kernel module** — vestigial; targets an input node that never fires.
* **Open questions** — block `0x03`, `0x0f` and `0x13` contents; the exact
  physical scale of the coordinates; whether the on-device file blocks
  (`0x09`, `0x0a`) can be read back.
