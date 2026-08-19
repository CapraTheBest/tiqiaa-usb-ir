"""
Tests for the Flipper `.ir` exporter.

Run: py -3 -m pytest tests/test_flipper_export.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir_dongle.flipper_export import ExportStats, _flipper_hex, render_key, render_remote
from ir_dongle.protocols import IRProtocol, ProtocolType


class FakeKey:
    def __init__(self, timings, freq=38000, type=0, name=""):
        self.timings = list(timings)
        self.freq = freq
        self.type = type
        self.name = name


def _nec(addr, cmd):
    return IRProtocol.encode_signal(ProtocolType.NEC, addr, cmd).timings


# --------------------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------------------

def test_names_fall_back_to_positional_placeholders():
    """With no caller-supplied names and no name on the key, buttons are Key_00, Key_01..."""
    keys = [FakeKey(_nec(4, c)) for c in range(3)]
    text, _ = render_remote(keys)
    names = [l.split(": ", 1)[1] for l in text.splitlines() if l.startswith("name: ")]
    assert names == ["Key_00", "Key_01", "Key_02"], names


def test_the_keys_own_name_is_used_when_present():
    keys = [FakeKey(_nec(4, 1), name="Power"), FakeKey(_nec(4, 2), name="Vol Up")]
    text, _ = render_remote(keys)
    names = [l.split(": ", 1)[1] for l in text.splitlines() if l.startswith("name: ")]
    assert names == ["Power", "Vol_Up"], names


def test_explicit_names_win():
    keys = [FakeKey(_nec(4, 1))]
    text, _ = render_remote(keys, names={0: "my button"})
    assert "name: my_button" in text


def test_duplicate_names_are_numbered():
    keys = [FakeKey(_nec(4, 1), name="Power"), FakeKey(_nec(4, 2), name="Power")]
    text, _ = render_remote(keys)
    names = [l.split(": ", 1)[1] for l in text.splitlines() if l.startswith("name: ")]
    assert names == ["Power", "Power_2"], names


# --------------------------------------------------------------------------------------
# record contents
# --------------------------------------------------------------------------------------

def test_flipper_hex_is_little_endian_bytes():
    assert _flipper_hex(0x07) == "07 00 00 00"
    assert _flipper_hex(0xBF00) == "00 BF 00 00"


def test_known_protocol_exports_as_parsed():
    stats = ExportStats()
    rec = render_key(FakeKey(_nec(0x04, 0x08)), 0, stats)
    assert "type: parsed" in rec and "protocol: NEC" in rec
    assert "address: 04 00 00 00" in rec and "command: 08 00 00 00" in rec
    assert stats.parsed == 1 and stats.raw == 0


def test_unknown_protocol_falls_back_to_raw_and_is_logged():
    """Nothing may be silently dropped: a fallback must be recorded with its reason."""
    stats = ExportStats()
    rec = render_key(FakeKey([224, 4848, 224, 7328] * 6), 0, stats)
    assert "type: raw" in rec and "data: " in rec
    assert stats.raw == 1 and len(stats.fallbacks) == 1
    assert "raw (" in stats.fallbacks[0]


def test_key_without_timings_is_skipped_and_logged():
    stats = ExportStats()
    assert render_key(FakeKey([]), 3, stats) is None
    assert stats.skipped == 1 and stats.skips


def test_header_and_record_separators():
    text, _ = render_remote([FakeKey(_nec(4, 1)), FakeKey(_nec(4, 2))])
    lines = text.splitlines()
    assert lines[0] == "Filetype: IR signals file"
    assert lines[1] == "Version: 1"
    assert lines.count("#") == 2


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
