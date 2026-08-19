"""
The 16us RLE codec and the wire-level sequence discipline.

Self-contained: no captures or external data needed.

Run: python -m pytest tests/test_rle_and_wire.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ir_dongle.tiqiaa_encode import decode_rle, encode_timings
from ir_dongle.tiqiaa_protocol import TiqiaaProtocol, _WireCounters


class _Recorder:
    """Stands in for the USB writer; records the bytes that would go on the wire."""

    def __init__(self):
        self.frames = []

    def bulk_transfer(self, data: bytes) -> int:
        self.frames.append(bytes(data))
        return len(data)


# --------------------------------------------------------------------------------------
# RLE codec
# --------------------------------------------------------------------------------------

def test_basic_round_trip():
    timings = [2400, 600, 1200, 600, 600, 600, 1200, 40000]
    back = decode_rle(encode_timings(timings))
    assert len(back) == len(timings)
    for a, b in zip(timings, back):
        assert abs(a - b) <= 16, f"{a} -> {b}"


def test_runs_on_a_multiple_of_127_units_do_not_fuse():
    """REGRESSION: a magnitude of 0x7f always means "the run continues", so it cannot also
    stand for a terminal value of 127. A run landing on an exact multiple of 127 units used
    to end on a bare continuation byte and swallow the run after it -- [2032, 600] came back
    as a single 2640us run."""
    for units in (127, 254, 381, 2413):
        back = decode_rle(encode_timings([units * 16, 608]))
        assert len(back) == 2, f"{units} units fused into {back}"


def test_no_encoded_run_ends_on_a_continuation_byte():
    for units in range(1, 600):
        rle = encode_timings([units * 16])
        assert (rle[-1] & 0x7F) != 0x7F, f"{units} units ends on a continuation byte"


def test_5c_and_5d_are_ordinary_space_bytes():
    """REGRESSION: 0x5c/0x5d were once believed to be repeat separators and were skipped on
    decode, silently deleting every 1472/1488us space. They are ordinary terminal bytes for
    a space of 92 / 93 units."""
    for us, byte in ((1472, 0x5C), (1488, 0x5D)):
        rle = encode_timings([608, us])
        assert rle[1] == byte, f"{us}us should encode to {byte:#04x}, got {rle[1]:#04x}"
        back = decode_rle(rle)
        assert len(back) == 2 and abs(back[1] - us) <= 16, back


def test_a_run_split_across_several_bytes_is_reassembled():
    """A run continues while the polarity bit is unchanged, so any chunking decodes the same."""
    assert decode_rle(bytes([0xFF, 0xFF, 0x95])) == decode_rle(bytes([0xFF, 0xC0, 0xD4]))


def test_polarity_alternates():
    """Marks are MSB-set, spaces MSB-clear, starting with a mark."""
    rle = encode_timings([600, 600, 600, 600])
    assert [bool(b & 0x80) for b in rle] == [True, False, True, False]


# --------------------------------------------------------------------------------------
# wire sequence discipline
# --------------------------------------------------------------------------------------

def _first_frames(frames):
    return [f for f in frames if f[4] == 1 and f[5:7] == b"ST"]


def _send(proto, n, rle=b"\xff\x95\x28\xc9"):
    seqs = []
    for _ in range(n):
        rec = _Recorder()
        assert proto.transmit_rle(rec, rle, freq_index=5)
        seqs.extend(f[7] for f in _first_frames(rec.frames))
    return seqs


def test_sequence_never_repeats_across_many_commands():
    """REGRESSION: the byte after "ST" is a per-command sequence counter, not a constant flag
    field. Pinning it made every command a byte-for-byte replay of the previous one, and the
    dongle stops accepting work once it sees a sequence it has already retired."""
    seqs = _send(TiqiaaProtocol(), 40)
    assert len(seqs) == len(set(seqs)), "a sequence byte repeated within 40 commands"


def test_sequence_survives_reconnect():
    """Closing the USB handle does not reset the dongle, so the counter must not restart."""
    first = _send(TiqiaaProtocol(), 3)
    second = _send(TiqiaaProtocol(), 3)
    assert not (set(first) & set(second)), (
        f"a reconnect replayed sequences {sorted(set(first) & set(second))}"
    )


def test_sequence_wraps_like_the_device_expects():
    c = _WireCounters()
    c._seq = 0xFD
    assert [c.next_seq() for _ in range(4)] == [0xFE, 0xFF, 0x00, 0x01]


def test_cmd_id_cycles_one_to_fifteen():
    c = _WireCounters()
    got = [c.next_cmd_id() for _ in range(17)]
    assert got[:15] == list(range(1, 16))
    assert got[15] == 1, f"cmd_id must wrap to 1, got {got[15]}"


def test_frames_are_chunked_at_0x38():
    rec = _Recorder()
    TiqiaaProtocol().transmit_rle(rec, b"\xff" * 200, freq_index=0)
    data = [f for f in rec.frames if f[1] != 0x09]
    assert len(data) > 1, "a 200-byte payload must span several frames"
    assert all(len(f) - 5 <= 0x38 for f in data), "a chunk exceeded 0x38 bytes"
    total = data[0][3]
    assert [f[4] for f in data] == list(range(1, total + 1)), "frame indices must be 1..total"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
