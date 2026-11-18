#!/usr/bin/env python3
"""Twitch Highlight Clipper – Startpunkt.

    python app.py                         # Weboberfläche starten (Browser öffnet sich)
    python app.py --test twitch.tv/name   # Testmodus in der Konsole (nur Chat)
    python app.py --replay logs/chat_….jsonl --threshold 2.5   # gespeicherten Chat auswerten
    python app.py --headless twitch.tv/name                     # ohne UI aufnehmen + clippen
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 oder neuer wird benötigt.")

from clipper.config import load_config
from clipper.logging_setup import setup_logging

log = logging.getLogger("clipper")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Twitch Highlight Clipper")
    p.add_argument("--config", default=None, help="Pfad zur config.yaml (Standard: neben app.py)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--test", metavar="LINK", help="Testmodus: nur Chat beobachten und Highlights loggen")
    mode.add_argument("--headless", metavar="LINK", help="Ohne Weboberfläche aufnehmen und clippen")
    mode.add_argument("--replay", metavar="JSONL", help="Gespeicherten Chat (aus dem Testmodus) erneut auswerten")
    p.add_argument("--threshold", type=float, help="Schwelle überschreiben (x-fach über Normal)")
    p.add_argument("--cooldown", type=float, help="Cooldown in Sekunden überschreiben")
    p.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    p.add_argument("--port", type=int, help="Port der Weboberfläche")
    p.add_argument("--debug", action="store_true", help="Ausführliches Logging")
    return p.parse_args(argv)


def run_until_ctrl_c(session) -> None:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    session.start()
    print("Läuft – beenden mit Strg+C.")
    try:
        while not stop.wait(0.5):
            pass
    finally:
        session.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    setup_logging("DEBUG" if args.debug else cfg.logging.level, cfg.resolve_path(cfg.logging.file))
    if args.threshold is not None:
        cfg.detection.threshold = args.threshold
    if args.cooldown is not None:
        cfg.detection.cooldown_seconds = args.cooldown
    if args.port:
        cfg.ui.port = args.port

    if args.replay:
        from clipper.replay import print_replay

        path = Path(args.replay)
        if not path.exists():
            log.error("Datei nicht gefunden: %s", path)
            return 1
        print_replay(path, cfg)
        return 0

    if args.test or args.headless:
        from clipper.session import Session, check_tools

        test_mode = bool(args.test)
        problems = check_tools(cfg, test_mode)
        if problems:
            for p in problems:
                log.error(p)
            return 1
        try:
            session = Session(cfg, args.test or args.headless, test_mode=test_mode)
        except ValueError as exc:
            log.error("%s", exc)
            return 1
        run_until_ctrl_c(session)
        return 0

    from clipper.ui import launch

    launch(cfg, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
