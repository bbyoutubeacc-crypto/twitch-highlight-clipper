"""Clips aus Puffer-Segmenten erzeugen (ffmpeg)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import ClipConfig
from .recorder import Segment
from .utils import subprocess_flags

log = logging.getLogger(__name__)

# 9:16-Ausschnitt aus der Bildmitte, auf 1080x1920 skaliert
VERTICAL_FILTER = "crop=trunc(ih*9/16/2)*2:ih,scale=1080:1920:flags=lanczos,setsar=1"


class FFmpegError(RuntimeError):
    pass


def run_ffmpeg(args: list[str], cwd: Path | None = None, timeout: float = 1800) -> None:
    """Führt ffmpeg aus und wirft bei Fehlern eine verständliche Exception."""
    log.debug("ffmpeg %s", " ".join(args[1:]))
    try:
        proc = subprocess.run(
            args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=timeout, **subprocess_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("ffmpeg hat zu lange gebraucht (Timeout).") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-6:]
        raise FFmpegError("ffmpeg-Fehler: " + " | ".join(tail))


def _encode_args(cfg: ClipConfig) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", cfg.video_preset, "-crf", str(cfg.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000",
        "-movflags", "+faststart",
    ]


def _concat_list_line(path: Path) -> str:
    # Format des concat-Demuxers: einfache Anführungszeichen escapen
    p = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{p}'"


def cut_from_segments(
    ffmpeg: str,
    segments: list[Segment],
    wall_start: float,
    wall_end: float,
    out_path: Path,
    cfg: ClipConfig,
) -> tuple[float, float]:
    """Fügt die Segmente zusammen und schneidet framegenau auf [wall_start, wall_end].

    Gibt den tatsächlich enthaltenen Zeitraum (Uhrzeit) zurück – er kann
    kürzer sein, wenn der Puffer nicht alles enthält.
    """
    if not segments:
        raise FFmpegError("Keine Segmente für diesen Zeitraum im Puffer.")
    segments = [s for s in segments if s.path.exists()]
    if not segments:
        raise FFmpegError("Segmente wurden bereits gelöscht.")
    first, last = segments[0], segments[-1]
    real_start = max(wall_start, first.wall_start)
    real_end = min(wall_end, last.wall_end)
    if real_end - real_start < 1.0:
        raise FFmpegError("Zu wenig Material im Puffer für diesen Clip.")
    offset = real_start - first.wall_start
    duration = real_end - real_start

    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text("\n".join(_concat_list_line(s.path) for s in segments) + "\n", encoding="utf-8")
    try:
        run_ffmpeg([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-ss", f"{offset:.3f}", "-t", f"{duration:.3f}",
            "-map", "0:v:0?", "-map", "0:a:0?",
            *_encode_args(cfg),
            str(out_path),
        ])
    finally:
        list_file.unlink(missing_ok=True)
    return real_start, real_end


def transform_clip(
    ffmpeg: str,
    src: Path,
    dst: Path,
    cfg: ClipConfig,
    vertical: bool = False,
    ass_file: Path | None = None,
) -> Path:
    """Erzeugt eine Variante des Clips: 9:16-Ausschnitt und/oder eingebrannte Untertitel."""
    filters: list[str] = []
    if vertical:
        filters.append(VERTICAL_FILTER)
    cwd = None
    if ass_file is not None:
        # Pfade im Filter sind unter Windows heikel (C:\ ...). Daher ffmpeg im
        # Ordner der .ass-Datei starten und nur den Dateinamen übergeben.
        cwd = ass_file.parent
        name = ass_file.name.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        filters.append(f"ass='{name}'")
    if not filters:
        raise ValueError("transform_clip ohne Änderung aufgerufen")
    run_ffmpeg([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src.resolve()),
        "-vf", ",".join(filters),
        "-map", "0:v:0?", "-map", "0:a:0?",
        *_encode_args(cfg),
        str(dst.resolve()),
    ], cwd=cwd)
    return dst


def make_thumbnail(ffmpeg: str, src: Path, dst: Path, at_seconds: float) -> Path | None:
    try:
        run_ffmpeg([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, at_seconds):.2f}", "-i", str(src),
            "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(dst),
        ], timeout=60)
        return dst if dst.exists() else None
    except FFmpegError as exc:
        log.warning("Vorschaubild fehlgeschlagen: %s", exc)
        return None
