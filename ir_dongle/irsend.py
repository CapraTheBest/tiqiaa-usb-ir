r"""
Send a button from a Flipper Zero `.ir` file through the dongle.

    irsend Sony_TV.ir Power                  # after `pip install .`
    irsend Sony_TV.ir --list
    irsend Sony_TV.ir Vol_up Vol_up Vol_dn   # several in sequence
    irsend Sony_TV.ir Power --repeats 6      # fewer in-frame repeats

    python -m ir_dongle.irsend Sony_TV.ir Power     # without installing

Each press is transmitted as the frame repeated `--repeats` times in ONE transmission, which
is what remotes actually do -- many receivers ignore a single frame. The send then blocks
until the dongle confirms the infrared has gone out; see ir_dongle/tiqiaa_protocol.py for why
that wait is not optional.
"""
import argparse
import os

from .device import find_tiqiaa_dongle
from .flipper_parser import load_ir_file
from .ir_decode import decode


class _Key:
    """Adapter: send_key wants .timings and .freq."""

    def __init__(self, signal):
        self.timings = signal.timings
        self.freq = signal.frequency


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("irfile", help="path to a Flipper Zero .ir file")
    ap.add_argument("buttons", nargs="*", help="button name(s) to send, in order")
    ap.add_argument("--list", action="store_true", help="list the buttons and exit")
    ap.add_argument("--repeats", type=int, default=10,
                    help="in-frame repeats per press (default 10)")
    args = ap.parse_args()

    signals = load_ir_file(args.irfile)
    if not signals:
        print(f"No signals found in {args.irfile}")
        return 1
    by_name = {s.name: s for s in signals}

    if args.list or not args.buttons:
        print(f"{len(signals)} buttons in {os.path.basename(args.irfile)}:\n")
        for s in signals:
            d = decode(s.timings, s.frequency) if s.timings else None
            detail = f"  {d}" if d and d.is_named else ""
            print(f"  {s.name:<24} {s.frequency:>6} Hz  {len(s.timings):>4} timings{detail}")
        return 0

    missing = [b for b in args.buttons if b not in by_name]
    if missing:
        print(f"Not in this file: {', '.join(missing)}")
        print("Use --list to see the available names.")
        return 1

    dongle = find_tiqiaa_dongle()
    if not dongle:
        print("No dongle found. Plugged in? On Windows, is WinUSB bound to 10C4:8468?")
        return 2

    try:
        for name in args.buttons:
            ok = dongle.send_key(_Key(by_name[name]), repeats=args.repeats)
            print(f"  {name:<24} {'sent' if ok else 'NOT ACKNOWLEDGED'}")
    finally:
        dongle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
