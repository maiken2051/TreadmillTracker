"""Command line: python -m gymtracker (or the `gymtracker` script).

So far only `--replay` exists: it runs a capture through the recorder and prints the state
changes and the resulting workout (`--table` prints the raw 1 Hz table instead). The full
app (BLE, session, storage, UI) replaces this in Phase 4.
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
    ap.add_argument("--table", action="store_true", help="with --replay: print the 1 Hz table")
    args = ap.parse_args(argv)
    if args.replay is None:
        ap.error("only --replay is implemented so far")
    # Replay prints its own state changes; only show warnings (e.g. unknown frames).
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    from gymtracker.sim import replay
    return replay.main([str(args.replay)] + (["--table"] if args.table else []))


if __name__ == "__main__":
    sys.exit(main())
