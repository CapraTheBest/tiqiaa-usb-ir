# Design notes

Why this driver is shaped the way it is. Most of it is the record of things that went wrong,
because every one of them is a trap the next implementation will fall into too.

---

## The dongle wedges, and only unplugging it helps

The symptom: send a handful of commands and the dongle stops emitting. The LED goes dark, USB
writes still "succeed", and nothing short of physically unplugging it brings it back.

Things that do **not** fix it, all measured:

| attempt | result |
|---|---|
| `clear_halt()` on both endpoints | still dead |
| `set_configuration()` again | still dead |
| full USB `reset()` | still dead |
| closing and reopening the handle | still dead |
| a different USB driver (WinUSB vs HID) | never the cause |

That last one cost the most time. The device enumerates as **interface class 3 (HID)** with two
bulk endpoints, so "Windows' HID driver is fighting us" is an extremely attractive theory. It is
wrong. The wedge happens with WinUSB cleanly bound and nothing else touching the device.

### The actual cause

**The protocol is synchronous, and the dongle only answers an IR data frame once the infrared
has physically been emitted.** A ten-repeat code takes about 600 ms to go out. Push the next
payload in before that reply lands and you corrupt the firmware mid-transmission — permanently,
until it loses power.

So the send path waits:

1. send-header → wait up to **500 ms** for its reply
2. if the device does not report ready → **abort, do not transmit**
3. send the IR data frames
4. wait up to **2000 ms** for the "transmitted" reply

Every step blocks. `send_key()` taking ~600 ms is not overhead — it is the device's real pace,
and going faster is what breaks it. Verified over 100+ consecutive commands on one connection.

### The trap within the trap

An earlier "fix" was to close and reopen the USB handle for every button press, on the theory
that reconnecting resets the device. It made things **worse**, because of the next section.

---

## Every frame carries a sequence byte

The byte after the `ST` marker is a per-command counter, `(n + 1) & 0xFF`. It is easy to mistake
for a constant flags field — in one capture it happened to read `05` on the frame everyone looks
at first.

It is not a flag. The dongle **echoes it back** in its reply:

```
we send : 02 09 01 01 01 53 54 01 4C 45 4E     ("ST" seq=01 'L' "EN")
it says : 01 0A 0B 01 01 53 54 01 4C 03 45 4E  ("ST" seq=01 'L' status "EN")
                          ^^ our sequence, returned
```

Hold it constant and every command becomes a byte-for-byte replay of the previous one. The
device retires a sequence once it has executed it, so after a few commands it simply stops
accepting work.

**The counter must also survive reconnects.** Closing the USB handle does not reset the dongle —
only removing power does. Restart the counter on every connection and you replay sequences it
has already retired, which is exactly why reopen-per-press made the wedge *more* frequent rather
than less. Hence the process-wide counters in `tiqiaa_protocol._WireCounters`.

There is a second, separate counter in the frame header (`cmd_id`) that cycles 1..15. Both are
needed; they are not the same thing.

---

## The IN endpoint must be drained continuously

A background thread reads the IN endpoint for the life of the connection. This is not
housekeeping — with nothing draining it the dongle acknowledges commands and **never fires the
LED**. It is also half of the flow control above: the thread is what receives the reply the
sender is blocked waiting for.

---

## A frame without its trailing gap decodes perfectly and transmits nothing

The protocol encoders originally emitted only a frame's own marks and spaces, ending on a ~600 µs
space. Repeat that ten times for one button press and you get ten frames with **no silence
between them**. A TV ignores the lot.

The cruel part: the signal still decodes correctly. Protocol, address and command all round-trip
exactly. Every offline test passed while the code was completely unusable on real hardware — it
took pointing it at an actual television to find.

`encode_signal()` now pads each frame to its protocol's standard period (SIRC 45 ms, NEC 108 ms,
RC5 114 ms). See `IRProtocol.FRAME_PERIOD_US`.

**Lesson: a decoder agreeing with your encoder proves nothing about whether the signal works.**

---

## RLE details that bite

The wire format is one byte per run: MSB = mark(1)/space(0), low 7 bits = count of 16 µs units.

**A run continues while the polarity bit is unchanged.** Decoding by "magnitude == 0x7f means
continue" instead is *almost* right and fails in two specific ways:

- A run landing on an exact multiple of 127 units ends on a bare `0x7f`, which reads as a
  continuation and **fuses with the run after it** — `[2032, 600]` comes back as one 2640 µs run.
  Real data hits this: some codes carry a 38,608 µs gap, which is exactly 19 × 127 units.
- `0x5c` and `0x5d` look like frame separators in a hex dump. They are not — they are ordinary
  terminal bytes for spaces of 92 and 93 units (1472 / 1488 µs). Treating them as separators
  drops the inter-frame gap, so every frame after the first comes out with its mark/space
  polarity inverted.

The polarity rule makes both impossible. When encoding, never emit a terminal byte of magnitude
`0x7f` — shorten the run by one unit (16 µs, far below any receiver's tolerance).

---

## It cannot receive

The protocol defines a learn mode: selector `'R'`, wait for a reply of type 5 carrying captured
IR, `'C'` to cancel. The firmware acknowledges `'R'` and correctly reports state 2 (*receiving*).

It still hears nothing. Controlled experiment, three 20-second phases:

| phase | frames returned |
|---|---|
| sensor covered, nothing pressed | 1 |
| uncovered, nothing pressed | 1 |
| remote held against it, pressed ~100× | 2 |

Covered and uncovered are **identical**, so nothing is arriving through the optics. Every frame
in all three phases is just the acknowledgement of our own `'R'` command; the one extra frame in
the last phase is a data message with an **empty payload** — the dongle explicitly reporting that
it captured nothing.

A false positive worth knowing about: reading the data reply *after a transmission* returns the
last **transmit** buffer, which looks exactly like a real capture and even reports the right
carrier frequency. The most convincing evidence of "it works" was our own data coming back.

These units also report a null serial (`00000000-0000-0000-0000-000000000001`), consistent with
clone hardware built without the receiver. **Transmit only.**

---

## Frame gaps are not a fixed threshold

Splitting a repeated transmission into frames on "any space > 5000 µs" breaks protocols whose
*data* spaces are longer than that — one real code uses 224 µs marks with 4848 µs = 0 and
7328 µs = 1, and a fixed cutoff shreds it into unusable fragments.

The rule used here: take the fixed cutoff, and fall back to a threshold derived from the signal's
own space distribution only when the fixed one produces a degenerate frame. That was chosen by
measuring both across ~75,000 real codes — the derived threshold *alone* was worse, dropping the
fully-decoded rate from 59.7% to 54.7%.

---

## Verification

Every claim above is a test in `tests/`. They are worth reading as documentation: each one names
the bug it prevents and how it originally showed up.
