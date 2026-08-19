"""
Export decoded remotes to Flipper Zero `.ir` files.

Every key is written in the best form it qualifies for:

  * `type: parsed` when the decoder identified a protocol Flipper itself implements, with
    address/command in Flipper's little-endian byte format. Compact and carrier-correct.
  * `type: raw` otherwise -- the microsecond timings verbatim, which still transmits fine.

NOTHING IS SILENTLY DROPPED. Every key that could not be exported as `parsed` is recorded
with the reason, so a caller can print or log exactly what fell back and why (a locked
project decision: "LOG every code that fails to parse so nothing is silently lost").

Button names come from the caller: pass an index->name map to render_remote/export_remote, or
each key falls back to its own `name` attribute and finally to Key_00, Key_01, ...
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .ir_decode import decode

# Protocols the Flipper firmware can synthesise itself. Anything else must go out as raw --
# writing a protocol name Flipper does not know makes the file unusable on the device.
FLIPPER_PROTOCOLS = {
    "NEC", "NECext", "NEC42", "NEC42ext", "Samsung32",
    "RC5", "RC5X", "RC6", "SIRC", "SIRC15", "SIRC20",
    "Kaseikyo", "RCA", "Pioneer",
}

DEFAULT_FREQ = 38000
DEFAULT_DUTY = 0.33


@dataclass
class ExportStats:
    parsed: int = 0
    raw: int = 0
    skipped: int = 0
    fallbacks: List[str] = field(default_factory=list)   # why each key went raw
    skips: List[str] = field(default_factory=list)       # why each key was unusable

    @property
    def total(self) -> int:
        return self.parsed + self.raw + self.skipped

    def summary(self) -> str:
        pct = (100.0 * self.parsed / self.total) if self.total else 0.0
        return (f"{self.total} keys: {self.parsed} parsed ({pct:.1f}%), "
                f"{self.raw} raw, {self.skipped} skipped")


def _flipper_hex(value: int) -> str:
    """Flipper writes integers as 4 space-separated little-endian hex bytes."""
    value &= 0xFFFFFFFF
    return " ".join(f"{(value >> (8 * i)) & 0xFF:02X}" for i in range(4))


_SAFE = re.compile(r"[^A-Za-z0-9_+-]")


def _clean_name(name: str) -> str:
    """Flipper button names must be plain ASCII with no spaces.

    Library names are frequently Chinese (or empty), so anything that does not survive
    ASCII-ification is rejected here and the caller falls back to the key type.
    """
    name = _SAFE.sub("_", (name or "").strip()).strip("_")
    name = re.sub(r"_+", "_", name)
    return name


def _button_name(key, index: int, override: Optional[str], typed: bool = True) -> str:
    """Best available name for a button: caller override, else the key's own name,
    else a positional placeholder."""
    if override:
        return _clean_name(override) or f"Key_{index:02d}"
    cleaned = _clean_name(getattr(key, "name", "") or "")
    return cleaned or f"Key_{index:02d}"


def _unique(name: str, used: Dict[str, int]) -> str:
    """Flipper tolerates duplicate names but they are useless to a human; number them."""
    if name not in used:
        used[name] = 1
        return name
    used[name] += 1
    return f"{name}_{used[name]}"


def render_key(key, index: int, stats: ExportStats,
               name_override: Optional[str] = None,
               used_names: Optional[Dict[str, int]] = None,
               typed: bool = True) -> Optional[str]:
    """Render one key as a Flipper `.ir` record, or None if it has nothing to transmit."""
    timings = list(getattr(key, "timings", None) or [])
    used_names = used_names if used_names is not None else {}
    name = _unique(_button_name(key, index, name_override, typed), used_names)

    if not timings:
        stats.skipped += 1
        stats.skips.append(f"{name}: no timings (blob failed to decode)")
        return None

    freq = int(getattr(key, "freq", 0) or 0) or DEFAULT_FREQ
    d = decode(timings, freq)

    if d.protocol in FLIPPER_PROTOCOLS and d.is_named:
        stats.parsed += 1
        return (f"name: {name}\n"
                f"type: parsed\n"
                f"protocol: {d.protocol}\n"
                f"address: {_flipper_hex(d.address)}\n"
                f"command: {_flipper_hex(d.command)}\n")

    stats.raw += 1
    why = d.protocol if d.protocol not in FLIPPER_PROTOCOLS else "no address/command"
    stats.fallbacks.append(f"{name}: raw ({why}"
                           + (f", {d.notes}" if d.notes else "") + ")")
    return (f"name: {name}\n"
            f"type: raw\n"
            f"frequency: {freq}\n"
            f"duty_cycle: {DEFAULT_DUTY}\n"
            f"data: {' '.join(str(int(t)) for t in timings)}\n")


def render_remote(keys: Iterable, names: Optional[Dict[int, str]] = None,
                  stats: Optional[ExportStats] = None) -> "tuple[str, ExportStats]":
    """Render a whole remote to `.ir` text. `names` maps key index -> button name."""
    stats = stats or ExportStats()
    names = names or {}
    keys = list(keys)
    used: Dict[str, int] = {}
    out = ["Filetype: IR signals file", "Version: 1"]
    for i, k in enumerate(keys):
        rec = render_key(k, i, stats, names.get(i), used)
        if rec:
            out.append("#")
            out.append(rec.rstrip("\n"))
    return "\n".join(out) + "\n", stats


def export_remote(remote, path: str, names: Optional[Dict[int, str]] = None) -> ExportStats:
    """Write one remote to `path` as a Flipper `.ir` file."""
    text, stats = render_remote(remote.keys, names)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return stats
