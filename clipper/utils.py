"""Kleine Hilfsfunktionen: Twitch-Links, externe Programme, Dateinamen."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

_CHANNEL_RE = re.compile(r"^[a-zA-Z0-9_]{2,25}$")


def parse_channel(link: str) -> str:
    """Extrahiert den Kanalnamen aus einem Twitch-Link oder Namen.

    Akzeptiert z. B. ``https://twitch.tv/name``, ``www.twitch.tv/name/``,
    ``m.twitch.tv/name?foo=bar``, ``twitch.tv/name/videos`` oder nur ``name``.
    """
    text = (link or "").strip()
    if not text:
        raise ValueError("Bitte einen Twitch-Link oder Kanalnamen eingeben.")
    text = re.sub(r"^[a-z]+://", "", text, flags=re.IGNORECASE)
    text = text.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in text.split("/") if p]
    if parts and re.match(r"^(www\.|m\.|go\.)?twitch\.tv$", parts[0], re.IGNORECASE):
        parts = parts[1:]
    if not parts:
        raise ValueError(f"Kein Kanalname im Link gefunden: {link!r}")
    name = parts[0].lstrip("@").lower()
    if parts[0].lower() in ("videos", "directory", "settings") or not _CHANNEL_RE.match(name):
        raise ValueError(f"Ungültiger Twitch-Kanal: {link!r}")
    return name


def channel_url(channel: str) -> str:
    return f"https://www.twitch.tv/{channel}"


def find_ffmpeg(configured: str = "") -> str:
    """Pfad zu ffmpeg (Konfiguration > PATH)."""
    if configured:
        if Path(configured).exists() or shutil.which(configured):
            return configured
        raise FileNotFoundError(f"ffmpeg nicht gefunden unter '{configured}' (recording.ffmpeg_path).")
    found = shutil.which("ffmpeg")
    if not found:
        raise FileNotFoundError(
            "ffmpeg wurde nicht gefunden. Bitte installieren und zum PATH hinzufügen (siehe README)."
        )
    return found


def streamlink_command(configured: str = "") -> list[str]:
    """Befehl, um streamlink zu starten (bevorzugt aus der aktuellen Python-Umgebung)."""
    if configured:
        if Path(configured).exists() or shutil.which(configured):
            return [configured]
        raise FileNotFoundError(f"streamlink nicht gefunden unter '{configured}' (recording.streamlink_path).")
    if importlib.util.find_spec("streamlink") is not None:
        return [sys.executable, "-m", "streamlink"]
    found = shutil.which("streamlink")
    if not found:
        raise FileNotFoundError("streamlink wurde nicht gefunden. Bitte 'pip install streamlink' ausführen.")
    return [found]


def safe_filename(text: str, max_len: int = 80) -> str:
    """Macht aus beliebigem Text einen Windows-/Mac-tauglichen Dateinamen."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return text[:max_len] or "clip"


def subprocess_flags() -> dict:
    """Unter Windows kein extra Konsolenfenster für ffmpeg/streamlink öffnen."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
