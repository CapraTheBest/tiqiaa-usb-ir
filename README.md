# tiqiaa-usb-ir

A Python driver for the **Tiqiaa / JOYROOM USB-C infrared dongle** — the little USB stick sold
as *JR-CY278*, *JR-IR02*, *Tiqiaa Tview* and a dozen other names
(`VID 0x10C4` or `0x045E`, `PID 0x8468`).

Plug it into a PC, load a Flipper Zero `.ir` file, and transmit. No Android phone, no vendor app.

```python
from ir_dongle.device import find_tiqiaa_dongle
from ir_dongle.flipper_parser import load_ir_file

signals = {s.name: s for s in load_ir_file("Sony_TV.ir")}
dongle  = find_tiqiaa_dongle()

class Key:                       # anything with .timings and .freq
    def __init__(self, s): self.timings, self.freq = s.timings, s.frequency

dongle.send_key(Key(signals["Power"]))
dongle.close()
```

## What it does

- **Transmits IR** through the dongle over raw USB (`pyusb`)
- **Reads and writes Flipper Zero `.ir` files**, both `parsed` and `raw` records
- **Encodes** NEC / NEC42 / Samsung32 / RC5 / RC5X / RC6 / SIRC 12·15·20
- **Decodes** raw mark/space timings back to `(protocol, address, command)` — structurally,
  so it copes with the timing variance real remotes actually have

## What it does not do

- **It does not receive.** The protocol defines a learn/receive mode and the firmware
  acknowledges it, but every unit tested has no receiver hardware — with a remote held against
  the sensor it reports an empty capture, and covering the sensor changes nothing. Transmit only.
- **It ships no IR codes.** Bring your own `.ir` files. [Flipper-IRDB][irdb] has thousands.
- **It has nothing to do with any vendor's code database.**

[irdb]: https://github.com/Lucaslhm/Flipper-IRDB

## Install

```bash
pip install .
# or, to work on it:
pip install -r requirements.txt
```

**Windows** additionally needs the WinUSB driver bound to the dongle — use [Zadig][zadig],
pick the `10C4:8468` device, install *WinUSB*. Note that plugging into a **different USB port**
creates a new device instance that may need Zadig again, and a Windows update can revert the
binding to HIDClass; if the dongle stops being found, check the driver first.

Linux needs a udev rule (or root) to open the device.

Developed and tested on **Windows 11**. Nothing in it is Windows-specific beyond that driver
step — `pyusb`/`libusb` cover Linux and macOS — but those are untested, so reports welcome.

[zadig]: https://zadig.akeo.ie/

## The two things that make this work

Both were found the hard way, and both are why a naive implementation fails:

### 1. The protocol is synchronous

The dongle only answers an IR data frame **once the infrared has physically been emitted** — a
10-repeat code takes about 600 ms. Push the next payload in before that reply arrives and the
firmware corrupts itself **unrecoverably**: `clear_halt`, `set_configuration` and a full USB
`reset()` all fail to bring it back. Only unplugging it does.

So every send waits for its acknowledgement, exactly like the vendor app does — 500 ms for a
control frame, 2000 ms for the transmission, and it refuses to send unless the device reports
ready. Verified over 100+ consecutive commands on a single connection.

### 2. Every frame carries a sequence byte

The byte after the `ST` marker is a per-command sequence counter, `(n + 1) & 0xFF`. The dongle
**echoes it back** in its reply. Reuse it and the device stops accepting work. It must also
keep advancing across reconnects — closing the USB handle does not reset the dongle's own
state, so restarting the counter replays sequences it has already retired.

## Wire format

```
02 <len> <cmd_id> <total> <index> | 53 54 <seq> <cmd> [args...] 45 4E
                                    "ST"            'D'          "EN"
```

- `cmd_id` cycles 1..15; `<len>` counts the bytes after itself; payload chunks are max `0x38`
- command letters come from `"LSRHOLCV"` — `L` poll/idle, `S` send, `R` receive, `O` done,
  `C` cancel, `V` identity
- IR payload is `ST <seq> D <freq_index> <RLE...> EN`
- **RLE**: one byte per run, MSB = mark(1)/space(0), low 7 bits = count of **16 µs** units.
  A run continues while the polarity bit is unchanged, so long runs split across several bytes.

Full detail in [docs/PROTOCOL.md](docs/PROTOCOL.md). The *why* behind the driver's design —
and the list of things that went wrong getting there — is in
[docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md).

## Tests

```bash
python -m pytest tests/ -q
```

They are the evidence the thing works — every one encodes a bug that actually bit, including
frames fusing when a run lands on a multiple of 127 units, RC5 decoding off by a half-bit, and
protocol encoders omitting the inter-frame gap (which decodes perfectly and transmits nothing a
TV will accept).

## Credits

The protocol was reverse-engineered independently from USB captures and the vendor app's own
native library. It was afterwards cross-checked against [iodn/android-ir-blaster][airb] (GPL-3.0),
an unrelated Android project that also drives this dongle — every constant agreed, and their
polarity-based RLE rule confirmed ours on all 75,000 codes we had to test against. No code was
taken from it; if you want an Android app for this hardware, use theirs.

[airb]: https://github.com/iodn/android-ir-blaster

## License

MIT — see [LICENSE](LICENSE).
