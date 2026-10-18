"""Command line: python -m gymtracker (or the `gymtracker` script).

Phase 1 only has `--replay`, which prints the 1 Hz table of a capture. The full app (BLE,
session, storage, UI) replaces this in Phase 4.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from gymtracker import __version__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gymtracker", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--replay", type=Path, metavar="FIXTURE",
                    help="replay a JSONL capture instead of using Bluetooth")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.replay is None:
        ap.error("only --replay is implemented so far")
    from gymtracker.sim import replay
    return replay.main([str(args.replay)])


if __name__ == "__main__":
    sys.exit(main())
