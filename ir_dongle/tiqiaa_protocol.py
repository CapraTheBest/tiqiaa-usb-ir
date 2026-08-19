"""
Tiqiaa USB IR Protocol Implementation

Reverse-engineered from USB captures of the dongle in normal use, then verified against real
hardware. See docs/PROTOCOL.md for the wire format.

POINTS THAT ARE EASY TO GET WRONG:
  * MARK = MSB SET (byte | 0x80); SPACE = MSB clear.  (was backwards -> inverted IR)
  * RLE unit = 16us via rounding (timing + 8) >> 4, after carrier scaling.
  * Payload = "ST"(53 54) <seq> "D"(44) <freq_index> <RLE...> "EN"(45 4e)
    (byte[2] is a per-command SEQUENCE byte, byte[4] is a carrier-FREQUENCY INDEX.)
  * cmd_id is ONE global counter shared by all frames, wraps 1..15 (< 0x10).
  * Long runs use 0x7f (space) / 0xff (mark) filler bytes.

THE MULTI-COMMAND WEDGE:
  The byte after "ST" is the per-command SEQUENCE counter, not a constant flag field. A
  capture of a working session shows it advancing monotonically across the whole session --
  01,02,03 (polls) 04 (send-header) 05..0x11 (13 IR commands) -- never repeating, and running
  past 0x0f so it is clearly not the 4-bit cmd_id. The dongle ECHOES it back in its reply.
  Hold it constant and every command becomes a byte-for-byte replay of the previous one; the
  dongle retires a sequence once executed, stops accepting new work after a few commands, and
  only a physical replug clears it (that power-cycles the firmware -- closing the USB handle
  does not reset its sequence state).

Supports JOYROOM JR-CY278 / Tiqiaa Tview (VID 0x10C4/0x045E PID 0x8468).
"""

import threading
from typing import List, Optional

ST = bytes([0x53, 0x54])         # "ST"
EN = bytes([0x45, 0x4E])         # "EN"
D = 0x44                         # "D"

# Carrier frequency index table (index 0..29 -> Hz, see device.py). Index 0 is the
# common 38kHz path (unit = 16us). We default to index 0.
FREQ_INDEX_DEFAULT = 0

# Our input is already real microseconds, so we simply quantise us -> units of 16 and do not
# apply any additional carrier scaling.


class _WireCounters:
    """The two on-the-wire counters, shared process-wide.

    These deliberately do NOT reset when a connection is closed. The dongle's firmware
    keeps its sequence state across a USB handle close -- only a physical replug clears it
    -- so restarting either counter per connection makes every reconnect replay sequences
    the device has already retired. That is what made the old reopen-per-command path send
    byte-identical frames forever.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cmd_id = 0x00
        self._seq = 0x00

    def next_cmd_id(self) -> int:
        """Frame header byte[2]: `next = cur + 1; if next >= 0x10: next = 1` (cycles 1..15)."""
        with self._lock:
            nxt = self._cmd_id + 1
            if nxt >= 0x10:
                nxt = 1
            self._cmd_id = nxt
            return nxt

    def next_seq(self) -> int:
        """The byte after "ST".

        A plain 8-bit counter, `(n + 1) & 0xFF` -- which is why it runs past 0x0f without
        wrapping, unlike the 4-bit cmd_id.
        """
        with self._lock:
            self._seq = (self._seq + 1) & 0xFF
            return self._seq

    def set_seq(self, value: int) -> None:
        """Force the sequence (so a capture can be reproduced byte-exactly in tests)."""
        with self._lock:
            self._seq = (value - 1) & 0xFF


# Process-wide counters: see _WireCounters for why these outlive a connection.
_COUNTERS = _WireCounters()


class TiqiaaProtocol:
    UNIT_US = 16
    MAX_DATA_PER_FRAME = 0x38     # 56 payload bytes per frame
    MARK_FILL = 0xFF             # mark run continuation (native uses -1 => 0xff for marks)
    SPACE_FILL = 0x7F           # space run continuation

    def __init__(self, counters: Optional[_WireCounters] = None):
        # Default to the shared counters so opening a new connection continues the
        # sequence instead of restarting it (see _WireCounters).
        self._counters = counters if counters is not None else _COUNTERS

    def _next_cmd_id(self) -> int:
        return self._counters.next_cmd_id()

    def _next_seq(self) -> int:
        return self._counters.next_seq()

    def set_sequence(self, value: int) -> None:
        """Set the next sequence byte to `value`. Used by tests to reproduce a capture."""
        self._counters.set_seq(value)

    # ---- RLE encoding (matches native _t) -------------------------------
    def _encode_timings(self, timings_us: List[int]) -> bytes:
        """Encode alternating mark/space (starting with MARK) into Tiqiaa RLE.
        MARK bytes have MSB set; SPACE bytes have MSB clear. unit = 16us with
        (t + 8) >> 4 rounding. Runs > 127 units emit filler bytes."""
        out = bytearray()
        for i, t_us in enumerate(timings_us):
            is_mark = (i % 2 == 0)
            units = (t_us + 8) >> 4            # round(t_us/16)
            if units <= 0:
                units = 1
            fill = self.MARK_FILL if is_mark else self.SPACE_FILL
            # emit full-127 filler bytes for the overflow, then the remainder
            while units > 0x7F:
                out.append(fill)
                units -= 0x7F
            val = units & 0x7F
            if is_mark:
                val |= 0x80
            out.append(val)
        return bytes(out)

    # ---- payload + framing ----------------------------------------------
    def _build_ir_payload(self, rle: bytes, freq_index: int,
                          seq: Optional[int] = None) -> bytes:
        # "ST" <seq> "D" <freq_index> <RLE> "EN"
        # seq=None means "take the next one" -- the normal path. An explicit value is only
        # for replaying a capture verbatim.
        if seq is None:
            seq = self._next_seq()
        return ST + bytes([seq & 0xFF, D, freq_index & 0xFF]) + rle + EN

    def _frame(self, payload: bytes) -> List[bytes]:
        """Chunk payload into frames: [02][len=chunk+3][cmd_id][total][index][chunk...]."""
        cmd_id = self._next_cmd_id()
        chunks = [payload[i:i + self.MAX_DATA_PER_FRAME]
                  for i in range(0, len(payload), self.MAX_DATA_PER_FRAME)]
        total = len(chunks)
        frames = []
        for idx, chunk in enumerate(chunks, start=1):
            frame = bytes([0x02, len(chunk) + 3, cmd_id, total & 0xFF, idx & 0xFF]) + chunk
            frames.append(frame)
        return frames

    def _build_handshake(self, selector: int = 0) -> List[bytes]:
        """Poll/identify: payload "ST" <seq> <sel-char> "EN", framed.

        The selector letter comes from the string "LSRHOLCV": 0=L poll/idle, 1=S send,
        2=R receive, 4=O output-done, 6=C cancel, 7=V identity.
        """
        sel_char = b"LSRHOLCV"[selector % 8]
        payload = ST + bytes([self._next_seq(), sel_char]) + EN
        return self._frame(payload)

    # ---- EXACT app control frames (from golden capture) -----------------
    # Fixed 11-byte control frames: 02 09 <cmd> 01 01 53 54 <seq> <X 45 4e>
    #   poll  cmd=01, suffix "LEN" (4c 45 4e)
    #   send  cmd=02, suffix "SEN" (53 45 4e)
    # These are emitted ONCE when the device is taken (3 polls + 1 send-header); IR data
    # commands then stream back to back with no control frames in between.
    # NOTE: byte[2] is a literal 0x01/0x02 on these control frames (that is what the golden
    # capture shows), so they must NOT consume a cmd_id tick -- only the sequence byte
    # advances. Burning a cmd_id here is what previously limited IR data frames to just 5
    # distinct cmd_ids (3,6,9,12,15) instead of the full 1..15.
    def _poll_frame(self) -> bytes:
        return bytes([0x02, 0x09, 0x01, 0x01, 0x01, 0x53, 0x54,
                      self._next_seq(), 0x4C, 0x45, 0x4E])

    def _send_header_frame(self) -> bytes:
        return bytes([0x02, 0x09, 0x02, 0x01, 0x01, 0x53, 0x54,
                      self._next_seq(), 0x53, 0x45, 0x4E])

    # ---- public API -----------------------------------------------------
    def transmit(self, device, frequency_hz: int, timings_us: List[int],
                 freq_index: int = FREQ_INDEX_DEFAULT, **kw) -> bool:
        """Transmit an IR signal from alternating mark/space timings (starts MARK).

        NOTE: this is for GENERATED codes (NEC/Flipper). For verbatim replay of a
        captured RLE payload, use transmit_rle() instead -- the captured format can
        contain byte sequences a timing round-trip does not reproduce exactly.
        (0x5c/0x5d were once believed to be repeat separators; they are ordinary 1472/1488us
        space bytes -- see tiqiaa_encode.decode_rle.)
        """
        if not timings_us:
            return False
        rle = self._encode_timings(timings_us)
        return self.transmit_rle(device, rle, freq_index=freq_index, **kw)

    # Reply-wait budgets: 500 ms for a control frame's reply, 2000 ms for the IR
    # transmission to finish and report back.
    CONTROL_REPLY_MS = 500
    DATA_REPLY_MS = 2000

    def transmit_rle(self, device, rle: bytes,
                     freq_index: int = FREQ_INDEX_DEFAULT,
                     poll_count: int = 1, read_replies: bool = False,
                     control_prefix: bool = True, wait: bool = True,
                     flags=None) -> bool:
        """Transmit a pre-encoded RLE IR payload verbatim (e.g. a captured code).

        Wire sequence (golden capture):
          1. POLL:        `02 09 01 ... 53 54 <seq> LEN`
          2. SEND-HEADER: `02 09 02 ... 53 54 <seq> SEN`
          3. IR DATA:     `02 LEN <cmd_id> <total> <idx> [ST <seq> D freqidx <RLE> EN]`

        FLOW CONTROL (`wait=True`, the default) -- this is what stops the dongle wedging.
        A command is never fired blindly. The sequence is:

            send-header          -> WAIT up to 500 ms for its reply
            if not ready         -> abort; do not transmit
            send the IR frames
                                 -> WAIT up to 2000 ms for the completion reply

        The dongle only answers an IR data frame once the IR has actually been emitted, and
        a 10-repeat code takes the better part of a second to send. Pushing the next payload
        in before that reply arrives corrupts the firmware mid-transmission and hard-wedges
        it -- no clear_halt, no set_configuration and not even a USB reset brings it back,
        only a physical replug (verified 2026-08-19). Waiting for the reply is the fix.

        Requires the device adapter to expose `wait_reply(timeout_ms)`; without it (e.g. an
        offline test double) the wait is skipped and behaviour falls back to fire-and-forget.

        `control_prefix=False` sends only the IR data frames, which is correct for every
        command after the first on a connection.

        `flags` is accepted only so old call sites keep working; it is ignored, because that
        byte is the sequence counter and is now managed here.

        NOTE: the dongle only EMITS IR if its IN endpoint is being drained, which the
        background _TiqiaaInReader thread does. `read_replies=True` drains IN synchronously
        instead, for callers running without that thread.
        """
        if not rle:
            return False
        read = getattr(device, "bulk_read", None) if read_replies else None
        waiter = getattr(device, "wait_reply", None) if wait else None
        if waiter is not None:
            # Clear any pending replies immediately before sending, so a stale one can never
            # be mistaken for this command's acknowledgement.
            clear = getattr(device, "clear_replies", None)
            if clear is not None:
                clear()
        try:
            if control_prefix:
                for _ in range(max(1, poll_count)):
                    device.bulk_transfer(self._poll_frame())
                    if read is not None:
                        read(64)
                    if waiter is not None:
                        waiter(self.CONTROL_REPLY_MS)
                device.bulk_transfer(self._send_header_frame())
                if read is not None:
                    read(64)
                if waiter is not None and waiter(self.CONTROL_REPLY_MS) is None:
                    # The device did not acknowledge the send-header. Bail out here
                    # rather than transmitting into a device that has not said it is ready.
                    return False
            payload = self._build_ir_payload(rle, freq_index)
            for fr in self._frame(payload):
                device.bulk_transfer(fr)
            if read is not None:
                read(64)
            if waiter is not None:
                # Block until the IR has actually gone out. Skipping this is what wedged it.
                return waiter(self.DATA_REPLY_MS) is not None
            return True
        except Exception as e:  # noqa: BLE001
            print(f"Transmission failed: {e}")
            return False
