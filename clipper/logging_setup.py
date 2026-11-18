"""Zentrales Logging: Konsole, rotierende Logdatei und Puffer für die Weboberfläche."""

from __future__ import annotations

import collections
import logging
import logging.handlers
import threading
from pathlib import Path

FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
DATEFMT = "%H:%M:%S"


class MemoryLogHandler(logging.Handler):
    """Hält die letzten N Logzeilen im Speicher (für die Log-Anzeige in der UI)."""

    def __init__(self, capacity: int = 300) -> None:
        super().__init__()
        self._lines: collections.deque[str] = collections.deque(maxlen=capacity)
        self._lock_lines = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # pragma: no cover - Logging darf nie abstürzen
            return
        with self._lock_lines:
            self._lines.append(line)

    def text(self, last: int = 80) -> str:
        with self._lock_lines:
            return "\n".join(list(self._lines)[-last:])


memory_handler = MemoryLogHandler()
_configured = False


def setup_logging(level: str = "INFO", logfile: Path | None = None) -> None:
    """Richtet das Logging einmalig ein (mehrfacher Aufruf ist harmlos)."""
    global _configured
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if _configured:
        return
    formatter = logging.Formatter(FORMAT, DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    memory_handler.setFormatter(formatter)
    root.addHandler(memory_handler)

    if logfile is not None:
        try:
            logfile.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                logfile, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"))
            root.addHandler(fh)
        except OSError as exc:
            root.warning("Logdatei %s kann nicht geschrieben werden: %s", logfile, exc)

    # Laute Bibliotheken etwas dämpfen
    for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "gradio", "matplotlib", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
