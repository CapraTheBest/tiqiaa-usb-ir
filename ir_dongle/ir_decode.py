"""
Protocol decoder: raw IR timings -> (protocol, address, command).

The library's 75k keys come from many manufacturers, and the DB stores IDEALISED timings
that differ per remote (NEC leaders show up as 9000/4500, 8900/4400, 8500/4200, 8200/4000...).
Matching against hardcoded constants therefore under-counts badly, so this works
STRUCTURALLY instead:

  1. Split into marks/spaces and drop the trailing inter-frame gap.
  2. Peel off a leader (a mark/space pair much longer than the body).
  3. Decide the modulation family from the body's shape:
       - constant marks + two distinct spaces  -> PULSE DISTANCE (NEC, Samsung, Kaseikyo...)
       - constant spaces + two distinct marks  -> PULSE LENGTH   (Sony SIRC)
       - every run a 1x/2x multiple of one unit -> BI-PHASE      (RC5, RC6)
  4. Recover the bits, then name the protocol from bit count + leader ratio + carrier and
     unpack address/command per its own field layout.

Anything that does not fit is reported as its family with the raw bits (still exportable as
Flipper `raw`), or UNKNOWN. Nothing is silently dropped -- callers can log every failure.

Tolerances are fractional because the sources disagree on absolute values; see TOL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Fractional tolerance when comparing two durations. IR receivers are forgiving and these
# are idealised values from many vendors, so this is deliberately loose.
TOL = 0.30
# A gap longer than this ends the frame (inter-frame space rather than a data bit).
FRAME_GAP_US = 5000


def _close(a: float, b: float, tol: float = TOL) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= tol * max(a, b)


def _cluster(values: List[int], tol: float = TOL) -> List[Tuple[float, int]]:
    """Group durations into (centre, count), largest group first."""
    groups: List[List[int]] = []
    for v in sorted(values):
        for g in groups:
            if _close(g[0], v, tol):
                g.append(v)
                break
        else:
            groups.append([v])
    out = [(sum(g) / len(g), len(g)) for g in groups]
    out.sort(key=lambda t: -t[1])
    return out


@dataclass
class Decoded:
    """One decoded IR frame.

    `protocol` is a specific name (NEC, SIRC15, RC5, ...) when the frame matched a known
    layout, otherwise the family (PulseDistance/PulseLength/BiPhase) or "Unknown".
    `address`/`command` are None unless a named protocol was identified.
    """
    protocol: str
    address: Optional[int] = None
    command: Optional[int] = None
    bits: int = 0
    raw_value: Optional[int] = None
    family: str = ""
    frames: int = 1
    notes: str = ""

    @property
    def is_named(self) -> bool:
        return self.address is not None and self.command is not None

    def __str__(self) -> str:
        if self.is_named:
            return f"{self.protocol} addr=0x{self.address:X} cmd=0x{self.command:X}"
        return f"{self.protocol} ({self.bits} bits)"


# --------------------------------------------------------------------------------------
# frame splitting
# --------------------------------------------------------------------------------------

def frame_gap_threshold(timings: List[int]) -> float:
    """Work out, for THIS signal, how long a space has to be to end a frame.

    A fixed threshold does not work. Some protocols carry data spaces far longer than any
    sensible constant -- e.g. a BenQ code in this library uses 224us marks with 4848us = 0
    and 7328us = 1 -- and a 5000us cutoff shreds those into 2-element fragments that decode
    as nothing. Others (NEC) separate frames with a 40000us gap.

    So: cluster the spaces and only treat the longest group as a separator when it stands
    clearly apart (>2.5x) from the next longest. Otherwise the signal has no inter-frame gap
    and every space is data.
    """
    spaces = timings[1::2]
    if not spaces:
        return float("inf")
    centres = sorted(c for c, _ in _cluster(spaces))
    if len(centres) < 2:
        return float("inf")               # one kind of space -> all data
    largest, second = centres[-1], centres[-2]
    if largest > 2.5 * second:
        return (largest + second) / 2.0
    return float("inf")


def split_frames(timings: List[int], gap: Optional[float] = None) -> List[List[int]]:
    """Split a repeated transmission into individual frames on its inter-frame gap.

    send_key() repeats a code ~10x in one payload, and captured blobs often contain several
    frames too, so classification must look at ONE frame.
    """
    if gap is None:
        gap = frame_gap_threshold(timings)
    frames, cur = [], []
    for i, t in enumerate(timings):
        cur.append(t)
        # only a SPACE (odd index) can end a frame
        if i % 2 == 1 and t >= gap:
            frames.append(cur)
            cur = []
    if cur:
        frames.append(cur)
    return [f for f in frames if len(f) >= 4]


def _strip_gap(frame: List[int], gap: Optional[float] = None) -> List[int]:
    """Drop a trailing inter-frame space so it is not mistaken for a data bit."""
    if gap is None:
        gap = frame_gap_threshold(frame)
    f = list(frame)
    while len(f) > 1 and len(f) % 2 == 0 and f[-1] >= gap:
        f.pop()
    return f


# --------------------------------------------------------------------------------------
# bit recovery
# --------------------------------------------------------------------------------------

def _bits_lsb_first(bits: List[int], start: int, n: int) -> int:
    """Assemble n bits from `start` in WIRE order, least-significant bit first."""
    v = 0
    for i in range(n):
        if start + i < len(bits) and bits[start + i]:
            v |= 1 << i
    return v


def _bits_msb_first(bits: List[int]) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def _decode_pulse_distance(marks, spaces) -> Optional[List[int]]:
    """Constant mark, short space = 0, long space = 1. Returns bits in WIRE order."""
    sp = _cluster(spaces)
    if not sp:
        return None
    if len(sp) == 1:                      # all spaces equal -> every bit the same
        zero = one = sp[0][0]
    else:
        zero, one = sorted([sp[0][0], sp[1][0]])
        if _close(zero, one):
            return None
    return [0 if abs(s - zero) <= abs(s - one) else 1 for s in spaces]


def _decode_pulse_length(marks, spaces) -> Optional[List[int]]:
    """Constant space, short mark = 0, long mark = 1 (Sony SIRC). Bits in WIRE order."""
    mk = _cluster(marks)
    if len(mk) < 2:
        return None
    a, b = sorted([mk[0][0], mk[1][0]])
    if _close(a, b):
        return None
    return [0 if abs(m - a) <= abs(m - b) else 1 for m in marks]


def _cells_to_bits(levels: List[int]) -> Optional[List[int]]:
    """Sample a half-bit level stream two at a time. Each cell must change level."""
    lv = list(levels)
    if len(lv) % 2:
        lv.append(0)                      # trailing half-bit implied by the silence after
    bits = []
    for i in range(0, len(lv) - 1, 2):
        first, second = lv[i], lv[i + 1]
        if first == second:
            return None                   # not a valid bi-phase cell
        bits.append(1 if (first == 0 and second == 1) else 0)
    return bits or None


def _decode_biphase(frame: List[int], unit: float) -> Optional[List[int]]:
    """Manchester: build a half-bit level stream, then sample each cell. Bits in wire order.

    PHASE ALIGNMENT: RC5's first start bit is a 1, i.e. space-then-mark, but a capture only
    begins at the first MARK -- the leading half-bit space is never recorded. So the natural
    alignment is one half-bit late and every cell decodes as invalid. We therefore try with a
    phantom leading space first (the physically correct reading) and fall back to the raw
    alignment for protocols that genuinely start on a mark.
    """
    levels: List[int] = []
    for i, t in enumerate(frame):
        n = int(round(t / unit))
        if n < 1 or n > 2:
            return None
        levels.extend([1 if i % 2 == 0 else 0] * n)
    if len(levels) < 4:
        return None
    # Prefer a reading that actually looks like RC5 (13-14 cells whose first bit is the
    # always-1 start bit) over merely the first one that parses.
    fallback = None
    for candidate in ([0] + levels, levels):
        bits = _cells_to_bits(candidate)
        if not bits:
            continue
        if len(bits) in (13, 14) and bits[0] == 1:
            return bits
        if fallback is None:
            fallback = bits
    return fallback


# --------------------------------------------------------------------------------------
# protocol naming
# --------------------------------------------------------------------------------------

def _name_pulse_distance(bits: List[int], lead_m: float, lead_s: float) -> Decoded:
    fam = "PulseDistance"
    nbits = len(bits)
    value = _bits_msb_first(bits)
    if nbits == 32:
        # NEC-family wire order: addr, ~addr, cmd, ~cmd -- each byte sent LSB-first.
        a0, a1, c0, c1 = (_bits_lsb_first(bits, o, 8) for o in (0, 8, 16, 24))
        if _close(lead_m, lead_s, 0.20):
            # equal-length leader halves: Samsung's 4500/4500 style
            if (c0 ^ c1) == 0xFF:
                return Decoded("Samsung32", a0, c0, 32, value, fam)
            return Decoded("Samsung32", a0, c0, 32, value, fam, notes="command checksum bad")
        if (a0 ^ a1) == 0xFF and (c0 ^ c1) == 0xFF:
            return Decoded("NEC", a0, c0, 32, value, fam)          # both inverses valid
        if (c0 ^ c1) == 0xFF:
            return Decoded("NECext", (a1 << 8) | a0, c0, 32, value, fam)
        return Decoded("NEC32", a0, c0, 32, value, fam, notes="no valid inverse fields")
    if nbits == 42:
        addr = _bits_lsb_first(bits, 0, 13)
        cmd = _bits_lsb_first(bits, 26, 8)
        return Decoded("NEC42", addr, cmd, 42, value, fam)
    if nbits == 48:
        # Kaseikyo / Panasonic: 16-bit vendor id, then device / subdevice / command.
        vendor = _bits_lsb_first(bits, 0, 16)
        cmd = _bits_lsb_first(bits, 32, 8)
        return Decoded("Kaseikyo", vendor, cmd, 48, value, fam)
    return Decoded(fam, None, None, nbits, value, fam)


def _name_pulse_length(bits: List[int]) -> Decoded:
    fam = "PulseLength"
    nbits = len(bits)
    value = _bits_msb_first(bits)
    # Sony SIRC: 7-bit command first, then a 5/8/13-bit address, all LSB-first on the wire.
    if nbits in (12, 15, 20):
        cmd = _bits_lsb_first(bits, 0, 7)
        addr = _bits_lsb_first(bits, 7, nbits - 7)
        name = {12: "SIRC", 15: "SIRC15", 20: "SIRC20"}[nbits]
        return Decoded(name, addr, cmd, nbits, value, fam)
    return Decoded(fam, None, None, nbits, value, fam)


def _name_biphase(bits: List[int], unit: float) -> Decoded:
    fam = "BiPhase"
    nbits = len(bits)
    value = _bits_msb_first(bits)
    # RC5 frame, MSB-first on the wire: S1, S2, T(oggle), A[5], C[6].
    # S1 is always 1. In RC5X, S2 is the INVERTED 7th command bit (so S2=0 => cmd |= 0x40).
    if nbits in (13, 14) and bits[0] == 1:
        b = bits if nbits == 14 else bits + [0]
        s2 = b[1]
        addr = _bits_msb_first(b[3:8])
        cmd = _bits_msb_first(b[8:14])
        if s2 == 0:
            return Decoded("RC5X", addr, cmd | 0x40, nbits, value, fam)
        return Decoded("RC5", addr, cmd, nbits, value, fam)
    return Decoded(fam, None, None, nbits, value, fam)


# --------------------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------------------

def decode_frame(frame: List[int], gap: Optional[float] = None) -> Decoded:
    """Classify ONE frame of alternating mark/space timings (starting with a mark)."""
    f = _strip_gap(frame, gap)
    if len(f) < 4:
        return Decoded("Unknown", notes="too short")

    marks = f[0::2]
    spaces = f[1::2]

    # --- bi-phase? every run must be 1x or 2x a single half-bit unit ---
    body_min = min(x for x in f if x > 0)
    if all(_close(t, body_min, TOL) or _close(t, 2 * body_min, TOL) for t in f):
        got = _decode_biphase(f, body_min)
        if got:
            return _name_biphase(got, body_min)

    # --- leader: a first mark/space clearly longer than what follows ---
    # Leader detection keys off the MARK only. Sony's leader space (600us) is identical to
    # its bit space, so also demanding a long space would miss every SIRC frame and shift
    # all of its bits by one.
    lead_m = lead_s = 0.0
    body = f
    if len(f) >= 6:
        rest_marks = f[2::2]
        if rest_marks:
            longest_typical_mark = max(c for c, _ in _cluster(rest_marks))
            if f[0] > 1.8 * longest_typical_mark:
                lead_m, lead_s = float(f[0]), float(f[1])
                body = f[2:]

    bmarks = body[0::2]
    bspaces = body[1::2]
    # Deliberately NO trimming of a trailing lone mark here: it means different things per
    # family. In pulse-distance it is the stop bit (harmless -- bits come from the spaces),
    # but in pulse-length it is the LAST DATA BIT, and dropping it turns every 12-bit SIRC
    # frame into 11 bits and shifts address/command.
    if not bmarks or not bspaces:
        return Decoded("Unknown", notes="no body")

    mark_groups = _cluster(bmarks)
    space_groups = _cluster(bspaces)
    marks_uniform = len(mark_groups) == 1
    spaces_uniform = len(space_groups) == 1

    if marks_uniform and not spaces_uniform:
        got = _decode_pulse_distance(bmarks, bspaces)
        if got:
            return _name_pulse_distance(got, lead_m, lead_s)
    if spaces_uniform and not marks_uniform:
        got = _decode_pulse_length(bmarks, bspaces)
        if got:
            return _name_pulse_length(got)
    # Both vary (or neither does): fall back on whichever carries two clean levels.
    if len(space_groups) == 2 and len(mark_groups) <= 2:
        got = _decode_pulse_distance(bmarks, bspaces)
        if got:
            d = _name_pulse_distance(got, lead_m, lead_s)
            d.notes = (d.notes + "; marks not uniform").strip("; ")
            return d
    if len(mark_groups) == 2:
        got = _decode_pulse_length(bmarks, bspaces)
        if got:
            d = _name_pulse_length(got)
            d.notes = (d.notes + "; spaces not uniform").strip("; ")
            return d
    return Decoded("Unknown", bits=len(bspaces), notes="no recognisable modulation")


def decode(timings: List[int], freq: int = 0) -> Decoded:
    """Classify a full transmission (which may repeat the same frame many times).

    Decodes the first frame and reports how many frames the payload held.
    """
    if not timings:
        return Decoded("Unknown", notes="empty")
    t = list(timings)
    # Frame-gap policy, measured over the whole 75k library rather than guessed:
    # the plain 5000us cutoff is right for the overwhelming majority, and switching
    # everything to the derived threshold LOSES ground (59.7% -> 54.7% fully decoded).
    # But 5000us destroys protocols whose data spaces are longer than that (a BenQ code
    # here uses 4848us = 0 and 7328us = 1), chopping them into unusable fragments.
    # So: take the fixed split, and only fall back to the derived threshold when the fixed
    # one produced a degenerate frame. Strictly better than either alone -- same decode
    # rate, 4,049 fewer Unknowns.
    gap = FRAME_GAP_US
    frames = split_frames(t, gap)
    if not frames or len(frames[0]) < 8:
        gap = frame_gap_threshold(t)
        frames = split_frames(t, gap)
    if not frames:
        return Decoded("Unknown", notes="no frames")
    d = decode_frame(frames[0], gap)
    d.frames = len(frames)
    return d
