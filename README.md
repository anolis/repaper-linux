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

It also writes `~/.config/repaper/modprobe-hid-iskn.conf`, because the
kernel driver cannot read the calibration file and takes the same bounds as
module parameters instead:

```sh
sudo cp ~/.config/repaper/modprobe-hid-iskn.conf /etc/modprobe.d/hid-iskn.conf
```

A measured sweep is also what confirms the coordinate scale. On the unit
this was developed against the surface came out as 158 x 215 mm at 100
units per millimetre, against a real A5 sheet of 148 x 210 mm — the excess
being the margin the magnet is still tracked across beyond the paper.

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

## Using it to draw

On X11 a tablet is an absolute pointer, so it drives the desktop cursor by
design — a Wacom behaves the same way. Across a multi-monitor desktop that
makes it unusable: the whole surface stretches over every screen, so a
centimetre of pen travel crosses a monitor, and the aspect ratio is wrong so
circles come out as ellipses.

Confine it to one output:

```sh
./repaper_map.py --list        # show monitors and the surface aspect
./repaper_map.py DP-5          # map onto one, keeping the aspect ratio
./repaper_map.py --reset       # back to the whole desktop
```

The aspect ratio is preserved by default, using the largest correctly
proportioned rectangle that fits, so shapes keep their proportions. Pass
`--stretch` to fill the monitor instead.

The mapping belongs to an X device that does not live as long as you would
expect. libinput creates the pen's *tool* device lazily, the first time a
stylus is detected, and destroys it when the driver reloads or the pen is
away for a while. Each new device starts with an identity matrix, so a
mapping applied once quietly lapses and the cursor goes back to roaming.
Run it with `--watch` to reapply automatically:

```sh
./repaper_map.py DP-5 --watch
```

That is the form to put in your session startup.

### Getting the applications to use it

Nothing appears in any application until the pen tool device exists, and it
only appears once a stylus has been detected. Touch the pen to the tablet
first, then check:

```sh
xinput list | grep Repaper
```

You want a line reading `ISKN Repaper Pen Pen (0) ... [slave pointer]`. The
plain `ISKN Repaper Pen` entry listed under keyboards is the parent tablet,
not the tool, and applications do not use it directly.

**GIMP** will not use a device it has not been told about: an unconfigured
device is routed through the core pointer, which really does make the pen
behave as a mouse. Open `Edit` -> `Input Devices`, select
`ISKN Repaper Pen Pen (0)`, set its Mode to `Screen`, and Save.

**Krita** picks up XI2 tablets on its own once the tool device exists.

### The five case buttons

For the buttons to reach applications, two pieces of system configuration
are needed beyond the driver:

```sh
sudo cp iskn-repaper.tablet /etc/libwacom/
sudo cp 99-iskn-repaper.rules /etc/udev/rules.d/
sudo cp 60-iskn-repaper-pad.conf /etc/X11/xorg.conf.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

The libwacom file stops the Xorg log reporting the tablet as unknown and
tells desktops its size and button count. The udev rule tags the pad device
`ID_INPUT_TABLET_PAD`, which udev's built-in classifier does not do for a
button-only device, so libinput would otherwise ignore it. The Xorg snippet
adds an `InputClass` matching tablet pads, which the stock
`40-libinput.conf` does not ship -- without it the pad is added by udev and
then dropped with no driver assigned. Xorg reads that at server start, so
log out and back in.

They arrive in block `0x08` as a single byte: one code per button when

To check the tablet independently of any application:

```sh
./repaper_canvas.py
```

This reads the pen straight from its evdev node, so what it shows is what
the driver produces with nothing in between. Ticking **Detach from desktop
cursor** floats the device off the core pointer: the pen stops moving your
mouse entirely while the canvas keeps drawing, which is what "canvas only"
means in practice.

### The five case buttons

pressed, and the same code plus 5 when released.

| Button | Press | Release |
| --- | --- | --- |
| 1 | `0x0a` | `0x0f` |
| 2 | `0x0b` | `0x10` |
| 3 | `0x0c` | `0x11` |
| 4 | `0x0d` | `0x12` |
| 5 | `0x0e` | `0x13` |

The driver puts them on a second input device, `ISKN Repaper Pad`, reporting
`BTN_0` to `BTN_4`. That is how tablet pads are conventionally exposed, and
it keeps the pen device advertising only what a stylus actually has. Bind
them to tools or brushes in the application's input settings.

### Pressure is binary, and that is the hardware

The pen is a passive magnet with no force sensor, so there is nothing to
measure. X delivers exactly two pressure values and nothing between them.
Strokes therefore have constant width with hard starts and stops, which
feels like drawing with a mouse even though the tablet path is working.

There is no software fix for this. What works instead is driving brush
dynamics from something the tablet does measure:

* **Krita** — in the brush editor, set Size and Opacity to follow *Drawing
  Speed* or *Tilt* rather than Pressure.
* **GIMP** — pick a dynamic based on Velocity or Direction instead of
  Pressure, and enable the device first under `Edit` → `Input Devices`,
  setting its Mode to `Screen`. A device left at the default `Disabled` is
  routed through the core pointer, which really does make it act as a mouse.

Tilt is the more useful of the two here: it varies smoothly and is
genuinely measured, whereas velocity is inferred.

## Drawings stored on the device

The tablet records sessions to internal storage. `repaper_gui.py` is a desktop
browser for them: it lists what is on the device, previews each drawing,
exports it, and deletes it.

```sh
./repaper_gui.py
```

The same operations are available from the command line:

```sh
./repaper_files.py --list
./repaper_files.py --all -o drawings/     # download everything
./repaper_files.py --svg drawings/*.iskn  # convert to SVG, no tablet needed
./repaper_files.py --delete 3             # prompts before deleting
```

Downloads are verified against the size in the file table, so a truncated
transfer is reported rather than silently written out.

**It is not a USB disk.** The device has one configuration exposing only HID
and CDC interfaces, with no mass-storage class and no alternate configuration
to switch into, so the drawings can only come off over the vendor protocol.
That turns out to be an advantage: the files are structured stroke records
rather than an opaque blob.

**There is no firmware update path.** The vendor library exposes
`getFirmwareVersion()` and nothing else — no DFU, flash or bootloader symbols,
and the outgoing command set is fully enumerated at `0x33`–`0x38`. Do not
guess at flash commands on hardware with no recovery mode.

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

### Disk operations

```text
b3 a5 e1 35 <req:u8> <code:u64le> <file_id:u16le> <arg:u32le> <crc16le>
```

Taken from `BlockDiskOperation` in the vendor library, which writes the block
type and each field at fixed offsets.

| Request | Meaning | Code |
| --- | --- | --- |
| 2 | import — download a file | 0 |
| 3 | remove — delete a file | `0x688e` |
| 4 | format — erase everything | `0x688e` |

The device rejects requests 3 and 4 without that confirmation code, and sends
no acknowledgement for either, so a delete is confirmed by re-reading the file
table. `repaper_files.py` implements 2 and 3; format is deliberately absent.

A download arrives as a stream of `0x0b` blocks, each a 13-byte header plus up
to 64 bytes of file content:

```text
id:u16le  reserved:u8  index:u32le  total:u32le  length:u16le
```

Everything is little endian. Both counters are genuinely 32-bit — a 43 kB file
needs 676 chunks, and reading them as 16-bit big endian happens to work only
while the high bytes are zero, then silently truncates the transfer to a
quarter of the file. The `length` field matters equally: the last chunk is
short unless the size divides by 64, and padding it corrupts the result.

### Stored file format

```text
<signature:3> <version:u8> <record>*
```

Each record is a block type byte followed by that block's payload, so records
are **variable length**, not a fixed stride: type `0x03` carries 2 bytes and
type `0x18` carries 14. Some files open with a `0x03` record and some do not,
so assuming a fixed stride desynchronises on exactly those that do.

Block `0x18` is the stored pen record. It never appears in the live stream,
which is why probing the device never revealed it. Its first three signed
16-bit fields are x, y and the contact height, and the height takes exactly two
values: `+300` while the tip is on the paper, and negative while lifted.
Strokes break wherever it goes negative.

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

`repaper_uinput.py` is the bridge and the calibration tool, `repaper_gui.py`
and `repaper_files.py` handle stored drawings. The rest are for protocol work:

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

81 tests covering the checksum, framing and resync, payload layouts, the
subscribe bitmask, tilt conversion, the proximity test, disk commands, chunk
reassembly and the stored file format. Pure stdlib, no dependencies, and no
tablet required.

## Kernel module

`hid-iskn.c` drives the tablet entirely over the vendor HID pipe. It
subscribes to the pen3d block with `hid_hw_output_report()`, parses frames
in `raw_event()`, and registers a single pen input device. No serial port
and no userspace daemon are involved.

```sh
make
sudo insmod ./hid-iskn.ko
```

If the tablet was already bound to `hid-generic`, hand it over:

```sh
DEV=$(basename /sys/bus/hid/devices/*:2C87:0001.*)
echo -n "$DEV" | sudo tee /sys/bus/hid/drivers/hid-generic/unbind
```

The driver core rebinds it automatically. Confirm with:

```sh
dmesg | grep hid-iskn
```

which should report `pen stream subscribed over HID (mask 0x0008)`.

It deliberately does not connect `HID_CONNECT_HIDINPUT`. Letting the
descriptor build its own input devices only produces a silent digitizer and
a mouse node that applications then try to use.

### Module parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `swap_xy` | on | exchange the axes; the protocol reports them transposed |
| `invert_y` | on | mirror Y; the protocol reports it upside down |
| `invert_x` | off | mirror X |
| `x_min`, `x_max`, `y_min`, `y_max` | measured | raw coordinate bounds |

The orientation defaults describe the tablet held normally. Tilt follows its
axis through a swap or inversion, so it stays consistent with motion.

```sh
sudo insmod ./hid-iskn.ko swap_xy=0 invert_y=0
```

## Status

* **Protocol** — framing, checksum, request table, subscribe bitmask, all
  three pen payloads, the disk command set and the stored file format
  confirmed against hardware.
* **Bridge** — subscribes correctly, emits position, tilt, hover distance
  and contact, rejects idle noise, reports axis resolution, drops the tool
  out of proximity, and reconnects across unplug.
* **Kernel module** — drives the pen over the vendor HID pipe; replaces the
  silent digitizer and the mouse node with one pen device.
* **Stored drawings** — listed, downloaded, verified against the file table,
  rendered to SVG and deleted, from a GUI or the command line.
* **Open questions** — contents of blocks `0x03`, `0x0f` and `0x13`; the
  trailing fields of the `0x18` record beyond position and contact; whether
  the coordinate scale of 100 units per millimetre is exact.
