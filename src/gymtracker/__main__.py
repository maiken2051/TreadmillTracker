"""Command line: python -m gymtracker (or the `gymtracker` script).

    gymtracker run [-v]                              the app: record from the devices
    gymtracker run --replay FIXTURE [--speed N]      the app, fed from a capture
    gymtracker --replay FIXTURE [--table] [--save]   run a capture through the recorder
    gymtracker import-fit FOLDER --label NAME        import old .fit workouts
    gymtracker sheet [-o FILE]                       rebuild Workouts.xlsx
    gymtracker list                                  list stored workouts
    gymtracker export                                copy everything to the NAS now
    gymtracker rebuild-db --out FILE [--reparse]     new database from the archive

`run` prints state changes and a 1 Hz status line (the screen UI arrives in Phase 6).
Ctrl-C saves a workout in progress. --data-dir overrides [storage] data_dir.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from gymtracker import __version__, units
from gymtracker.config import Config, load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gymtracker", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[2:]))
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", type=Path, help="config.toml (default: built-in defaults)")
    ap.add_argument("--data-dir", type=Path, help="override [storage] data_dir")
    ap.add_argument("--replay", type=Path, metavar="FIXTURE",
                    help="replay a JSONL capture instead of using Bluetooth")
    ap.add_argument("--table", action="store_true", help="with --replay: print the 1 Hz table")
    ap.add_argument("--save", action="store_true",
                    help="with --replay: save the workout to the library")
    sub = ap.add_subparsers(dest="command")
    p = sub.add_parser("run", help="run the app (headless for now)")
    p.add_argument("--replay", type=Path, metavar="FIXTURE", dest="run_replay",
                   help="feed the app from a JSONL capture instead of Bluetooth")
    p.add_argument("--speed", type=float, default=1.0,
                   help="with --replay: times real time (0 = as fast as possible)")
    p.add_argument("-v", "--verbose", action="store_true", help="log connection details")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="no 1 Hz status line (the service: the journal gets state changes)")
    p = sub.add_parser("import-fit", help="import a folder of .fit workouts")
    p.add_argument("folder", type=Path)
    p.add_argument("--label", required=True, help="source label, e.g. walkingtall")
    p = sub.add_parser("sheet", help="rebuild the spreadsheet")
    p.add_argument("-o", "--out", type=Path)
    sub.add_parser("list", help="list stored workouts")
    sub.add_parser("export", help="copy the archive, spreadsheet and database to the NAS")
    p = sub.add_parser("rebuild-db", help="build a new database from the archive")
    p.add_argument("--out", type=Path, required=True, help="new database file (must not exist)")
    p.add_argument("--archive", type=Path, help="archive folder (default: <data_dir>/archive)")
    p.add_argument("--reparse", action="store_true",
                   help="re-derive workouts from their raw BLE notifications")
    args = ap.parse_args(argv)

    verbose = getattr(args, "verbose", False)
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
                        if verbose else "%(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.data_dir is not None:
        cfg.storage.data_dir = str(args.data_dir)

    if args.replay is not None:
        return _replay(cfg, args)
    if args.command == "run":
        from gymtracker.app import run
        # Status lines must reach `| tee` as they happen, not in 8 KB blocks.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)
        return run(cfg, args.run_replay, args.speed, quiet=args.quiet)
    if args.command == "import-fit":
        return _import_fit(cfg, args)
    if args.command == "sheet":
        with _library(cfg) as lib:
            print(lib.write_sheet(args.out))
        return 0
    if args.command == "list":
        return _list(cfg)
    if args.command == "export":
        return _export(cfg)
    if args.command == "rebuild-db":
        return _rebuild(cfg, args)
    ap.print_help()
    return 2


def _library(cfg: Config):
    from gymtracker.library import Library
    return Library(cfg.storage.data_dir, cfg.user.units, cfg.export.spreadsheet)


def _replay(cfg: Config, args) -> int:
    from gymtracker.sim import replay
    notes = replay.read_fixture(args.replay)
    if args.table:
        lines = replay.format_table(replay.table_rows(notes))
    else:
        lines = replay.summarize(notes, cfg.session)
    for line in lines:
        print(line)
    if args.save:
        from gymtracker.recorder import Recorder
        saved = []
        replay.run_recorder(notes, Recorder(cfg.session, on_save=saved.append))
        raw = [{"t": round(n.t * 1000), "dev": n.dev, "char": n.char, "data": n.data.hex()}
               for n in notes]
        with _library(cfg) as lib:
            for session in saved:
                print(f"saved as workout {lib.add_session(session, raw)}")
            lib.write_sheet()
    return 0


def _import_fit(cfg: Config, args) -> int:
    with _library(cfg) as lib:
        result = lib.import_fit(args.folder, args.label)
        lib.write_sheet()
    print(f"added {len(result.added)}, already there {len(result.duplicates)}, "
          f"failed {len(result.failed)}")
    for path, why in result.failed:
        print(f"  {path.name}: {why}")
    return 1 if result.failed else 0


def _list(cfg: Config) -> int:
    imperial = cfg.user.units == "imperial"
    with _library(cfg) as lib:
        for w in lib.store.list():
            s = w.summary
            dist = (f"{units.m_to_miles(s.distance_m):5.2f} mi" if imperial
                    else f"{s.distance_m / 1000:5.2f} km")
            moving = int(s.moving_s)
            print(f"{w.id:4}  {w.start_local:%Y-%m-%d %H:%M}  {moving // 60:3}:{moving % 60:02d}"
                  f"  {dist}  HR {s.avg_hr or '-':>3}  {w.source}")
    return 0


def _export(cfg: Config) -> int:
    from gymtracker.export import Exporter
    exporter = Exporter(cfg.storage.data_dir, cfg.export)
    result = exporter.export()
    for name in result.copied:
        print(f"  {name}")
    if not result.ok:
        print(f"NOT copied to {exporter.target}: {result.error} (queued for retry)")
        return 1
    print(f"{exporter.target} is up to date ({len(result.copied)} files copied)")
    return 0


def _rebuild(cfg: Config, args) -> int:
    from gymtracker.library import ARCHIVE_DIR, rebuild_db
    archive = args.archive or Path(cfg.storage.data_dir).expanduser() / ARCHIVE_DIR
    count, problems = rebuild_db(archive, args.out, reparse=args.reparse, cfg=cfg.session)
    print(f"rebuilt {count} workouts into {args.out}")
    for path, why in problems:
        print(f"  {path.name}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
