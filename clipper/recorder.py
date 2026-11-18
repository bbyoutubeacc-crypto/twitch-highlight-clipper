"""Stream-Aufnahme in einen Ringpuffer.

    streamlink --stdout <url> best  |  ffmpeg -f segment (10-s-Stücke, -c copy)

* ffmpeg schreibt eine CSV-Liste fertiger Segmente mit Stream-Zeiten (PTS).
* Daraus wird die echte Uhrzeit jedes Segments berechnet:
  ``Uhrzeit = Anker + PTS`` mit ``Anker = Median(Fertigstellungszeit - PTS-Ende)``
  über die letzten Segmente. Der Median glättet die stoßweise Lieferung
  von HLS-Daten.
* Alte Segmente werden gelöscht – außer sie werden gerade für einen Clip
  gebraucht (``protect()``).
* Endet streamlink/ffmpeg (Stream offline, Netzwerkfehler …), wird nach
  einer Wartezeit automatisch neu gestartet.
"""

from __future__ import annotations

import collections
import logging
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import RecordingConfig
from .utils import find_ffmpeg, streamlink_command, subprocess_flags

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Segmente & Puffer
# ---------------------------------------------------------------------------
@dataclass
class RecordingRun:
    """Ein ununterbrochener streamlink/ffmpeg-Lauf (eigene Zeitbasis)."""

    run_id: int
    started_wall: float
    _anchors: collections.deque[float] = field(default_factory=lambda: collections.deque(maxlen=10))

    def add_anchor_estimate(self, est: float) -> None:
        self._anchors.append(est)

    @property
    def anchor(self) -> float:
        return statistics.median(self._anchors) if self._anchors else self.started_wall


@dataclass
class Segment:
    path: Path
    run: RecordingRun
    pts_start: float
    pts_end: float

    @property
    def duration(self) -> float:
        return self.pts_end - self.pts_start

    @property
    def wall_start(self) -> float:
        return self.run.anchor + self.pts_start

    @property
    def wall_end(self) -> float:
        return self.run.anchor + self.pts_end


class SegmentBuffer:
    """Thread-sichere Liste der fertigen Segmente."""

    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._segments: list[Segment] = []
        self._protect: dict[int, float] = {}
        self._protect_ids = 0

    def add(self, seg: Segment) -> None:
        with self._lock:
            self._segments.append(seg)
            self._lock.notify_all()

    def all(self) -> list[Segment]:
        with self._lock:
            return list(self._segments)

    def clear(self) -> None:
        with self._lock:
            self._segments.clear()

    # ------------------------------------------------------------ Schutz
    def protect(self, from_wall: float) -> int:
        """Verhindert das Löschen von Segmenten, die nach ``from_wall`` enden."""
        with self._lock:
            self._protect_ids += 1
            self._protect[self._protect_ids] = from_wall
            return self._protect_ids

    def release(self, token: int) -> None:
        with self._lock:
            self._protect.pop(token, None)

    def cleanup(self, now: float, keep_seconds: float) -> int:
        """Löscht Segmente, die älter als ``keep_seconds`` und nicht geschützt sind."""
        with self._lock:
            limit = now - keep_seconds
            if self._protect:
                limit = min(limit, min(self._protect.values()))
            keep, drop = [], []
            for seg in self._segments:
                (drop if seg.wall_end < limit else keep).append(seg)
            self._segments = keep
        for seg in drop:
            try:
                seg.path.unlink(missing_ok=True)
            except OSError as exc:  # unter Windows evtl. noch geöffnet
                log.debug("Segment %s konnte nicht gelöscht werden: %s", seg.path.name, exc)
        return len(drop)

    # ------------------------------------------------------------ Abfragen
    def latest_wall_end(self) -> float | None:
        with self._lock:
            return self._segments[-1].wall_end if self._segments else None

    def buffered_seconds(self) -> float:
        with self._lock:
            return sum(s.duration for s in self._segments)

    def wait_until_covered(self, wall_time: float, timeout: float, stop: threading.Event | None = None) -> bool:
        """Blockiert, bis ein Segment bis ``wall_time`` reicht (oder Timeout)."""
        deadline = time.time() + timeout
        with self._lock:
            while True:
                if self._segments and self._segments[-1].wall_end >= wall_time:
                    return True
                remaining = deadline - time.time()
                if remaining <= 0 or (stop is not None and stop.is_set()):
                    return False
                self._lock.wait(timeout=min(remaining, 1.0))

    def segments_for(self, wall_start: float, wall_end: float) -> list[Segment]:
        """Lückenlose Segmente desselben Laufs, die [wall_start, wall_end] überdecken.

        Gibt es mehrere Läufe im Zeitraum (Stream-Abbruch), wird der Lauf
        genommen, der den größten Teil des Zeitraums abdeckt.
        """
        with self._lock:
            hits = [s for s in self._segments if s.wall_end > wall_start and s.wall_start < wall_end]
        if not hits:
            return []
        by_run: dict[int, list[Segment]] = collections.defaultdict(list)
        for s in hits:
            by_run[s.run.run_id].append(s)

        def overlap(segs: list[Segment]) -> float:
            return sum(min(s.wall_end, wall_end) - max(s.wall_start, wall_start) for s in segs)

        return sorted(max(by_run.values(), key=overlap), key=lambda s: s.pts_start)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------
class StreamRecorder:
    """Nimmt einen Twitch-Kanal kontinuierlich in den Ringpuffer auf."""

    def __init__(
        self,
        stream_url: str,
        config: RecordingConfig,
        buffer_dir: Path,
        on_status: Callable[[str], None] | None = None,
        input_command: list[str] | None = None,
    ) -> None:
        """``input_command`` ersetzt streamlink (nur für Tests: muss MPEG-TS
        auf stdout schreiben)."""
        self.url = stream_url
        self.cfg = config
        self.dir = buffer_dir
        self.buffer = SegmentBuffer()
        self.on_status = on_status
        self._input_command = input_command
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._procs: list[subprocess.Popen] = []
        self._run_counter = 0
        self.status = "aus"
        self.live = False
        self.quality_info = ""
        self._sl_log: collections.deque[str] = collections.deque(maxlen=30)
        self._ff_log: collections.deque[str] = collections.deque(maxlen=30)
        self.ffmpeg = find_ffmpeg(config.ffmpeg_path)
        self.streamlink = streamlink_command(config.streamlink_path) if input_command is None else []

    # ----------------------------------------------------------- Steuerung
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self._wipe_dir()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, clear_buffer: bool = True) -> None:
        self._stop.set()
        self._kill_procs()
        if self._thread:
            self._thread.join(timeout=10)
        self.live = False
        self._set_status("aus")
        if clear_buffer:
            self.buffer.clear()
            self._wipe_dir()

    def _wipe_dir(self) -> None:
        for p in list(self.dir.glob("run*_*.ts")) + list(self.dir.glob("run*.csv")):
            try:
                p.unlink()
            except OSError:
                pass

    def _set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            if self.on_status:
                self.on_status(text)

    # ----------------------------------------------------------- Hauptschleife
    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            self._run_counter += 1
            run = RecordingRun(run_id=self._run_counter, started_wall=time.time())
            got_data = False
            try:
                got_data = self._record_once(run)
            except FileNotFoundError as exc:
                log.error("%s", exc)
                self._set_status(f"Fehler: {exc}")
                return
            except Exception:
                log.exception("Unerwarteter Fehler in der Aufnahme")
            finally:
                self._kill_procs()
                self.live = False
            if self._stop.is_set():
                break

            offline = any("no playable streams" in l.lower() or "is offline" in l.lower() for l in self._sl_log)
            if offline:
                wait = self.cfg.offline_retry_seconds
                log.info("Stream ist offline – nächster Versuch in %.0f s.", wait)
                self._set_status(f"Stream offline – neuer Versuch in {wait:.0f} s")
            else:
                if got_data:
                    backoff = 5.0
                wait = backoff
                tail = (list(self._sl_log)[-3:] + list(self._ff_log)[-3:]) or ["(keine Ausgabe)"]
                log.warning("Aufnahme beendet – Neustart in %.0f s. Letzte Meldungen: %s", wait, " | ".join(tail))
                self._set_status(f"Verbindung verloren – Neustart in {wait:.0f} s")
                backoff = min(backoff * 2, 60.0)
            self._stop.wait(wait)

    def _build_commands(self, run: RecordingRun) -> tuple[list[str], list[str], Path]:
        prefix = f"run{run.run_id:03d}"
        csv_path = self.dir / f"{prefix}.csv"
        if self._input_command is not None:
            src = list(self._input_command)
        else:
            src = [
                *self.streamlink, "--stdout", "--loglevel", "info",
                *self.cfg.streamlink_args, self.url, self.cfg.quality or "best",
            ]
        ff = [
            self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-fflags", "+genpts+discardcorrupt",
            "-i", "pipe:0",
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c", "copy",
            "-f", "segment",
            "-segment_time", f"{self.cfg.segment_seconds:g}",
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            "-segment_list", str(csv_path),
            "-segment_list_type", "csv",
            str(self.dir / f"{prefix}_%05d.ts"),
        ]
        return src, ff, csv_path

    def _record_once(self, run: RecordingRun) -> bool:
        src_cmd, ff_cmd, csv_path = self._build_commands(run)
        self._sl_log.clear()
        self._ff_log.clear()
        self._set_status("prüfe Stream …")
        log.info("Starte Aufnahme (Lauf %d): %s", run.run_id, " ".join(src_cmd[-2:]) if src_cmd else "")
        flags = subprocess_flags()
        src = subprocess.Popen(src_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **flags)
        ff = subprocess.Popen(ff_cmd, stdin=src.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **flags)
        if src.stdout:
            src.stdout.close()  # ffmpeg bekommt EOF, wenn streamlink endet
        self._procs = [src, ff]
        threading.Thread(target=self._pump, args=(src.stderr, self._sl_log, "streamlink"), daemon=True).start()
        threading.Thread(target=self._pump, args=(ff.stderr, self._ff_log, "ffmpeg"), daemon=True).start()

        lines_done = 0
        got_data = False
        last_cleanup = 0.0
        while not self._stop.is_set():
            new_segments, lines_done = self._read_csv(csv_path, lines_done, run)
            for seg in new_segments:
                self.buffer.add(seg)
                if not got_data:
                    got_data = True
                    self.live = True
                    log.info("Aufnahme läuft – erstes Segment im Puffer.")
                self._set_status(f"nimmt auf{(' (' + self.quality_info + ')') if self.quality_info else ''}")
            now = time.time()
            if now - last_cleanup > 2:
                self.buffer.cleanup(now, self.cfg.buffer_seconds)
                last_cleanup = now
            if ff.poll() is not None:
                # restliche Segmente noch einlesen
                new_segments, lines_done = self._read_csv(csv_path, lines_done, run)
                for seg in new_segments:
                    self.buffer.add(seg)
                break
            if src.poll() is not None and not got_data and src.returncode != 0:
                break
            self._stop.wait(0.5)
        return got_data

    def _read_csv(self, csv_path: Path, done: int, run: RecordingRun) -> tuple[list[Segment], int]:
        try:
            content = csv_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return [], done
        lines = content.split("\n")
        complete = lines[:-1]  # letzte Zeile evtl. noch unvollständig
        now = time.time()
        result: list[Segment] = []
        for line in complete[done:]:
            parts = line.strip().rsplit(",", 2)
            if len(parts) != 3:
                continue
            name, start_s, end_s = parts
            try:
                start, end = float(start_s), float(end_s)
            except ValueError:
                continue
            run.add_anchor_estimate(now - end)
            result.append(Segment(path=self.dir / name.strip('"'), run=run, pts_start=start, pts_end=end))
        return result, len(complete)

    def _pump(self, stream, sink: collections.deque[str], name: str) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                sink.append(line)
                low = line.lower()
                if self._stop.is_set():
                    log.debug("%s: %s", name, line)  # Abbruchmeldungen beim Stoppen
                elif name == "streamlink":
                    if "opening stream" in low:
                        # z. B. "[cli][info] Opening stream: 1080p60 (hls)"
                        self.quality_info = line.split("Opening stream:", 1)[-1].strip()
                        log.info("streamlink: %s", line)
                    elif "error" in low or "ad" in low.split() or "pre-roll" in low:
                        log.info("streamlink: %s", line)
                    else:
                        log.debug("streamlink: %s", line)
                else:
                    log.debug("ffmpeg: %s", line)
        except (OSError, ValueError):
            pass

    def _kill_procs(self) -> None:
        procs, self._procs = self._procs, []
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except OSError:
                    pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except OSError:
                    pass
