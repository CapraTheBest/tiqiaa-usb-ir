# USB-C IR Dongle — wire protocol

Derived from USB captures of the dongle in normal use and verified against real hardware
(100+ consecutive commands on one connection, driving a Sony KDL-46W905A).
Cross-checked afterwards against an unrelated implementation, iodn/android-ir-blaster --
every constant agreed. Endpoint **0x01 OUT**, replies on **0x81 IN**.

## Device
- VID `0x10C4` (Silicon Labs) **or** `0x045E`; PID `0x8468`.
- All traffic is **bulk transfer** on endpoint **0x01 (OUT)**. Replies read on the IN endpoint.

## Frame format (on the wire)
```
[0] 0x02                 marker
[1] len                  number of bytes following this byte (so total = len + 2)
[2] cmd_id               command counter, increments per command (cyclic)
[3] total                total number of frames in this command
[4] index                1-based frame index
... frame 1 ALSO has:  53 54 ("ST")  <seq>  44 ("D")  <freq_index>
... then RLE IR bytes (split across frames at ~0x38 data bytes/frame)
... last frame ends with: 45 4e ("EN")
```

### Control frames (no IR payload), len byte = 0x09
- **Handshake / poll** (emitted by `s(int,int)` while connected):
  `02 09 01 01 01 53 54 NN 4c 45 4e`   ("ST" ... "LEN", type byte = 01)
- **Send-header** (immediately before IR data frames):
  `02 09 02 01 01 53 54 NN 53 45 4e`   ("ST" ... "SEN", type byte = 02)

## IR timing encoding (RLE)  ✅ DECODED
- **Unit = 16 µs per count.**
- Each byte: `magnitude = byte & 0x7F`. If `magnitude == 0x7F` the run **continues**
  into the next byte (accumulate), i.e. 0x7F is a "max chunk, more follows" marker.
  (0xFF = 0x7F continue too; high bytes like 0xc9/0xa3 are real values whose low-7 bits
  are the magnitude. The MSB is the mark/space flag, not part of the magnitude.)
- Runs **alternate mark / space**, starting with a mark.
- **`0x5c` / `0x5d` are NOT repeat separators** (corrected 2026-08-19). They are ordinary
  terminal bytes for a SPACE of 92 / 93 units (1472 / 1488 us). The golden capture reads
  `7f 7f 7f 7f 5c ff 95 ...` = continuation bytes accumulating the long inter-frame space,
  terminated by `5c`, then `ff 95` = the 2368us leader mark of the next frame. Skipping them
  drops that gap, so frames restart at ODD indices and every frame after the first comes out
  with mark/space inverted. Verified: as terminal bytes the golden frames start at
  0, 26, 52, 78; as "separators" they start at 0, 25, 50, 75.
- A **terminal byte can never have magnitude 0x7f**, since that always means "continue".
  A run landing on an exact multiple of 127 units must be shortened by one unit (16us) or it
  fuses with the run that follows.
- Decoded Power button → classic **NEC**: ~640µs mark, 560µs space = bit 0,
  ~1168µs space = bit 1, long ~2368µs leader, big trailing gap (long 0x7f run).

To encode: `count = round(timing_us / 16)`, then emit as bytes of ≤0x7F, using 0x7F
fillers for counts > 127 (each 0x7F = 127 units, final byte = remainder).

## The byte after "ST" is a SEQUENCE counter  ✅ (corrected 2026-08-19)
Every frame carries `53 54 <seq> ...`. `<seq>` is a per-command transaction id, **not** a
flags or count field:
- It is a plain 8-bit counter: `seq = (seq + 1) & 0xFF`.
- It is supplied per command and written straight after the "ST" marker, in both the
  control frames and the IR data frame.
- The capture shows it marching `01,02,03` (polls) `04` (send-header) `05..0x11` (13 IR
  commands) — past `0x0f`, so it is not the 4-bit `cmd_id`.
- **The dongle echoes it back**: sending poll `seq=01` returns `...53 54 01 4c 03...`.

Modelling it as a constant `flags=5` made every IR command a byte-identical replay.

## Required SEND sequence — and it is SYNCHRONOUS
A command is never fired blindly. The sequence is:
```
send-header            -> wait up to 500 ms for its reply
if state != ready      -> abort, do not transmit
send IR data frames
                       -> wait up to 2000 ms for the "transmitted" reply
```
1. Poll / identify (`02 09 01 01 01 53 54 NN 4c 45 4e`), wait for the reply.
2. Send-header (`02 09 02 01 01 53 54 NN 53 45 4e`), wait for the reply, check ready.
3. IR data as chunked frames (`02 LEN cmd_id total index ...`), frame 1 carrying
   `53 54 <seq> 44 <freq_index>`, last frame ending `45 4e`.
4. **Wait for the completion reply before sending anything else.**

Step 4 is not optional. The dongle only answers a data frame once the IR has physically
been emitted (a 10-repeat code takes ~600 ms). Sending the next payload before that reply
arrives corrupts the firmware **unrecoverably** — `clear_halt`, `set_configuration` and a
full USB `reset()` all fail; only a physical replug brings it back. This was the cause of
the long-standing "wedges after a few commands" bug (fixed and verified over 100+
consecutive commands on one connection, 2026-08-19).

## Reply format
`01 <len> <cmd_id> 01 01 53 54 <seq> <letter> <status> 45 4e`, where `<letter>` mirrors the
request (`L` poll, `S` send-header, `O` IR transmitted) and `<seq>` echoes the frame being
acknowledged. Observed: poll → `4c 03`, send-header → `53 09`, data → `4f 09`.
