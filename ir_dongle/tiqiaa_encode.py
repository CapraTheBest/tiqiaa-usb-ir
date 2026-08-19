"""
Encode IR mark/space timings (microseconds) into the verbatim Tiqiaa 16us RLE byte stream
that `device.send_ir_rle` / `TiqiaaProtocol.transmit_rle` transmit.

It is the bridge from a list of microsecond mark/space timings -- however you obtained them:
a Flipper `.ir` file, your own capture, a protocol encoder -- to the bytes the dongle sends.

RLE model (reverse-engineered from USB captures, and confirmed by decoding a capture of a
button press that demonstrably operated a TV):
  * unit = 16 us; count = round(t_us / 16) = (t_us + 8) >> 4
  * each emitted byte carries 7 magnitude bits; MSB SET = MARK, MSB CLEAR = SPACE.
  * a run longer than 0x7f units is split across several bytes of the SAME polarity: emit
    0x7f-magnitude continuation bytes (0xff for a mark, 0x7f for a space) then the remainder.
Runs always alternate mark, space, mark, ... starting with a MARK.

Verified: re-encoding a decoded capture reproduces the captured bytes exactly.

NOTE ON CARRIER ALIGNMENT: these are flat microsecond timings (e.g. 2400/600/1200). Some
sources store timings already rounded toward whole carrier cycles (2368/640/1168). The
difference is ~6% and tolerant receivers accept either; a picky one may not.
"""
from typing import List

UNIT_US = 16
MARK_BIT = 0x80
CONT = 0x7F  # max magnitude per byte; also the "continue" chunk value


def encode_timings(timings_us: List[int]) -> bytes:
    """Encode alternating mark/space timings (starting with MARK) into Tiqiaa RLE bytes."""
    out = bytearray()
    for i, t_us in enumerate(timings_us):
        is_mark = (i % 2 == 0)
        units = (t_us + 8) >> 4
        if units <= 0:
            units = 1
        # A magnitude of 0x7f ALWAYS means "run continues into the next byte", so it cannot
        # also stand for a terminal value of 127. A run landing on an exact multiple of 127
        # units would otherwise end on a bare continuation byte and fuse with the run that
        # follows -- e.g. [2032, 600] decoding back as a single 2640us run.
        # Real data hits this: 275 library keys carry a 38608us gap (2413 = 19 x 127 units).
        # Shortening by one unit (16us) keeps the stream unambiguous; the error is far below
        # any receiver's tolerance.
        if units % CONT == 0:
            units -= 1
        msb = MARK_BIT if is_mark else 0
        # full-chunk continuation bytes for the overflow
        while units > CONT:
            out.append(CONT | msb)
            units -= CONT
        out.append((units & CONT) | msb)
    return bytes(out)


def decode_rle(rle: bytes) -> List[int]:
    """Inverse of encode_timings: Tiqiaa RLE bytes -> list of us timings (mark, space, ...).

    NOTE on 0x5c / 0x5d -- these are NOT repeat separators (2026-08-19 correction; PROTOCOL.md
    and this function both had it wrong). They are ordinary terminal bytes for a SPACE of 92 or
    93 units (1472 / 1488 us). In the golden capture the byte sequence is

        7f 7f 7f 7f 5c ff 95 ...

    i.e. continuation bytes accumulating the long inter-frame space, terminated by 0x5c, then
    `ff 95` = the 2368us leader mark of the next frame. Skipping 0x5c dropped that gap, so
    frames restarted at ODD indices (0, 25, 50, 75) and every frame after the first came out
    with its mark/space polarity inverted; reading it as a terminal byte gives 0, 26, 52, 78.
    It also silently deleted 5,666 legitimate 1472/1488us spaces across the library.

    CONTINUATION RULE: a run continues while the POLARITY BIT (MSB) is unchanged, and a
    polarity flip terminates it. That is what iodn/android-ir-blaster does
    (TiqiaaUsbLearner.decodePreviewPattern), and it is strictly more robust than keying on
    "magnitude == 0x7f": it copes with a run split into arbitrary chunks, and it makes both
    of the bugs found on 2026-08-19 structurally impossible (a terminal 0x7f cannot be
    confused with a continuation, and 0x5c/0x5d cannot be mistaken for separators).
    Verified equivalent to the old rule on all 75,386 library keys and on the golden capture.
    """
    if not rle:
        return []
    out: List[int] = []
    polarity = bool(rle[0] & MARK_BIT)
    acc = rle[0] & CONT
    for x in rle[1:]:
        p, mag = bool(x & MARK_BIT), x & CONT
        if p == polarity:
            acc += mag
        else:
            if acc > 0:
                out.append(max(1, acc * UNIT_US))
            polarity, acc = p, mag
    if acc > 0:
        out.append(max(1, acc * UNIT_US))
    return out


if __name__ == "__main__":
    # round-trip sanity against the golden working capture
    golden = bytes.fromhex(
        "ff9528c928a328c927a327c927a427a328c927a428a327a327a3")
    timings = decode_rle(golden)
    print("decoded golden head (us):", timings[:8])
    re_enc = encode_timings(timings)
    print("re-encoded == golden:", re_enc == golden, re_enc.hex())
