"""Steuert eine Beobachtungs-Sitzung: Chat → Erkennung → Aufnahme → Clip.

Threads:
* ``chat``      – liest den Twitch-Chat (TwitchChatClient)
* ``recorder``  – streamlink/ffmpeg-Ringpuffer (nicht im Testmodus)
* ``ticker``    – ruft jede Sekunde den Detektor auf
* ``cutter``    – schneidet erkannte Highlights möglichst schnell aus dem
                  Puffer (damit nichts gelöscht wird, bevor es gesichert ist)
* ``processor`` – langsame Schritte: 9:16, Untertitel, YouTube-Texte, Upload
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .chat import ChatMessage, TwitchChatClient
from .clip_maker import FFmpegError, cut_from_segments, make_thumbnail, transform_clip
from .config import AppConfig
from .detection import ChatActivitySignal, HighlightDetector, HighlightEvent
from .recorder import StreamRecorder
from .subtitles import Transcriber, build_cues, write_ass, write_srt
from .utils import channel_url, find_ffmpeg, parse_channel, safe_filename
from .youtube import build_metadata, upload_available, upload_video, write_metadata_files

log = logging.getLogger(__name__)


class JobState(str, enum.Enum):
    WAITING = "wartet auf Aufnahme"
    CUTTING = "schneide"
    PROCESSING = "verarbeite"
    TRANSCRIBING = "Untertitel"
    UPLOADING = "lade hoch"
    DONE = "fertig"
    TEST = "Testmodus (nicht aufgenommen)"
    FAILED = "Fehler"


@dataclass
class ClipJob:
    event: HighlightEvent
    channel: str
    moment: float                  # Uhrzeit des Moments in der Aufnahme
    clip_start: float
    clip_end: float
    state: JobState = JobState.WAITING
    score: float = 0.0
    folder: Path | None = None
    base_name: str = ""
    files: dict[str, Path] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    youtube_ids: dict[str, str] = field(default_factory=dict)
    protect_token: int | None = None
    language: str = ""
    moment_offset: float = 0.0      # Position des Moments im Clip (Sekunden)

    @property
    def preview(self) -> Path | None:
        for key in ("landscape_subbed", "shorts_subbed", "landscape", "shorts"):
            if key in self.files and self.files[key].exists():
                return self.files[key]
        return None

    @property
    def top_chat(self) -> str:
        items = self.summary.get("top_emotes", [])[:3] or self.summary.get("top_messages", [])[:3]
        return ", ".join(f"{i['text']} ({i['users']})" for i in items)


@dataclass
class ClipOptions:
    """Einstellungen, die in der UI pro Sitzung geändert werden können."""

    pre_seconds: float
    post_seconds: float
    threshold: float
    stream_delay: float
    subtitles: bool
    format: str  # "16:9", "9:16", "both"

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "ClipOptions":
        return cls(
            pre_seconds=cfg.clips.pre_seconds,
            post_seconds=cfg.clips.post_seconds,
            threshold=cfg.detection.threshold,
            stream_delay=cfg.recording.stream_delay_seconds,
            subtitles=cfg.subtitles.enabled,
            format=cfg.clips.format,
        )


class Session:
    def __init__(
        self,
        cfg: AppConfig,
        link: str,
        test_mode: bool = False,
        options: ClipOptions | None = None,
        transcriber: Transcriber | None = None,
        recorder_input_command: list[str] | None = None,
    ) -> None:
        self.channel = parse_channel(link)
        self.cfg = cfg.copy()
        self.test_mode = test_mode
        if options is not None:
            self._apply_options(options)
        for w in self.cfg.validate():
            log.warning(w)
        self.started_at = 0.0
        self.running = False
        self.jobs: list[ClipJob] = []
        self._jobs_lock = threading.Lock()
        self._stop = threading.Event()
        self._cut_queue: queue.Queue[ClipJob | None] = queue.Queue()
        self._proc_queue: queue.Queue[ClipJob | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._processor_thread: threading.Thread | None = None
        self.display_name = self.channel
        self._chat_file = None
        self.transcriber = transcriber

        self.chat_signal = ChatActivitySignal(self.cfg.detection)
        self.detector = HighlightDetector(
            signals=[self.chat_signal],
            weights=self.cfg.detection.signal_weights,
            threshold=self.cfg.detection.threshold,
            cooldown_seconds=self.cfg.detection.cooldown_seconds,
            on_highlight=self._on_highlight,
        )
        self.chat = TwitchChatClient(self.channel, self._on_message, self.cfg.chat)
        self.recorder: StreamRecorder | None = None
        self.ffmpeg = ""
        if not test_mode:
            self.ffmpeg = find_ffmpeg(self.cfg.recording.ffmpeg_path)
            self.recorder = StreamRecorder(
                channel_url(self.channel), self.cfg.recording, self.cfg.buffer_path / self.channel,
                input_command=recorder_input_command,
            )

    # ------------------------------------------------------------ Optionen
    def _apply_options(self, o: ClipOptions) -> None:
        self.cfg.clips.pre_seconds = float(o.pre_seconds)
        self.cfg.clips.post_seconds = float(o.post_seconds)
        self.cfg.detection.threshold = float(o.threshold)
        self.cfg.recording.stream_delay_seconds = float(o.stream_delay)
        self.cfg.subtitles.enabled = bool(o.subtitles)
        self.cfg.clips.format = o.format

    def update_options(self, o: ClipOptions) -> None:
        """Einstellungen während des Laufs ändern (gilt für neue Highlights)."""
        old = ClipOptions.from_config(self.cfg)
        self._apply_options(o)
        for w in self.cfg.validate():
            log.debug(w)
        self.detector.set_threshold(self.cfg.detection.threshold)
        if old != ClipOptions.from_config(self.cfg):
            log.info(
                "Einstellungen: Schwelle %.1fx, Clip -%.0fs/+%.0fs, Delay %.1fs, Format %s, Untertitel %s",
                self.cfg.detection.threshold, self.cfg.clips.pre_seconds, self.cfg.clips.post_seconds,
                self.cfg.recording.stream_delay_seconds, self.cfg.clips.format,
                "an" if self.cfg.subtitles.enabled else "aus",
            )

    def set_threshold(self, value: float) -> None:
        self.cfg.detection.threshold = float(value)
        self.detector.set_threshold(value)
        log.info("Schwelle geändert auf %.1f", value)

    # ------------------------------------------------------------ Start/Stopp
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.started_at = time.time()
        self.chat_signal.mark_started(self.started_at)
        mode = "TESTMODUS (nur Chat)" if self.test_mode else "Aufnahme + Clips"
        log.info("Starte Sitzung für #%s – %s", self.channel, mode)
        log.info(
            "Einstellungen: Schwelle %.1fx, Clip -%.0fs/+%.0fs, Delay %.0fs, Format %s, Untertitel %s",
            self.cfg.detection.threshold, self.cfg.clips.pre_seconds, self.cfg.clips.post_seconds,
            self.cfg.recording.stream_delay_seconds, self.cfg.clips.format,
            "an" if self.cfg.subtitles.enabled else "aus",
        )
        if self.test_mode and self.cfg.logging.save_chat_in_test_mode:
            path = self.cfg.resolve_path("logs") / f"chat_{self.channel}_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._chat_file = path.open("a", encoding="utf-8")
            log.info("Chat wird gespeichert in %s (für --replay)", path)
        self.running = True
        self.chat.start()
        if self.recorder:
            self.recorder.start()
        self._spawn(self._ticker, "ticker")
        self._spawn(self._cutter, "cutter")
        if not self.test_mode:
            self._spawn(self._processor, "processor")

    def _spawn(self, fn, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)
        if name == "processor":
            self._processor_thread = t

    def wait_for_processing(self, timeout: float = 1800) -> None:
        """Wartet, bis die Clip-Verarbeitung im Hintergrund abgeschlossen ist."""
        t = self._processor_thread
        if t is not None and t.is_alive() and not self.running:
            log.info("Warte auf die Fertigstellung der letzten Clips … (Strg+C zum Abbrechen)")
            t.join(timeout=timeout)

    def stop(self, wait_for_processing: bool = True, timeout: float = 1800) -> None:
        """Beendet Chat und Aufnahme. Bereits erkannte Highlights werden noch
        aus dem Puffer geschnitten; die weitere Verarbeitung (Untertitel …)
        wird abgewartet (``wait_for_processing``) oder läuft im Hintergrund
        weiter."""
        if not self.running:
            return
        log.info("Stoppe Sitzung …")
        self._stop.set()
        self.chat.stop()
        # Offene Highlights noch so weit wie möglich fertig schneiden lassen
        self._cut_queue.put(None)
        for t in self._threads:
            if t.name == "cutter":
                t.join(timeout=120)
        if self.recorder:
            self.recorder.stop(clear_buffer=True)
        self._proc_queue.put(None)
        pending = [j for j in self.jobs_snapshot() if j.state in (
            JobState.PROCESSING, JobState.TRANSCRIBING, JobState.UPLOADING)]
        for t in self._threads:
            if t.name == "processor":
                if pending and wait_for_processing:
                    log.info("Warte auf die Fertigstellung von %d Clip(s) …", len(pending))
                    t.join(timeout=timeout)
                elif pending:
                    log.info("%d Clip(s) werden im Hintergrund fertig verarbeitet.", len(pending))
            elif t.name != "cutter":
                t.join(timeout=5)
        self._threads.clear()
        if self._chat_file:
            self._chat_file.close()
            self._chat_file = None
        self.running = False
        log.info("Sitzung beendet. %d Highlights erkannt.", len(self.jobs))

    # ------------------------------------------------------------ Callbacks
    def _on_message(self, msg: ChatMessage) -> None:
        if msg.display_name and msg.user == self.channel:
            self.display_name = msg.display_name
        self.chat_signal.add_message(msg)
        if self._chat_file:
            try:
                self._chat_file.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
            except (OSError, ValueError):
                pass

    def _ticker(self) -> None:
        next_tick = time.time()
        while not self._stop.is_set():
            self.detector.tick(time.time())
            next_tick += 1.0
            self._stop.wait(max(0.0, next_tick - time.time()))

    def _on_highlight(self, event: HighlightEvent) -> None:
        c, r = self.cfg.clips, self.cfg.recording
        moment = event.trigger_time - r.stream_delay_seconds
        job = ClipJob(
            event=event, channel=self.channel, moment=moment,
            clip_start=moment - c.pre_seconds, clip_end=moment + c.post_seconds, score=event.score,
        )
        if self.recorder:
            # Segmente sofort vor dem Löschen schützen
            job.protect_token = self.recorder.buffer.protect(job.clip_start)
        else:
            job.state = JobState.TEST
        with self._jobs_lock:
            self.jobs.append(job)
        self._cut_queue.put(job)

    # ------------------------------------------------------------ Schneiden
    def _chat_range(self, job: ClipJob) -> tuple[float, float]:
        """Zeitraum im Chat, der zum Clip-Inhalt gehört (Clip + Delay)."""
        d = self.cfg.recording.stream_delay_seconds
        return job.clip_start + d, job.clip_end + d

    def _cutter(self) -> None:
        while True:
            job = self._cut_queue.get()
            if job is None:
                return
            try:
                self._cut(job)
            except Exception as exc:
                job.state = JobState.FAILED
                job.error = str(exc)
                log.exception("Highlight #%d fehlgeschlagen", job.event.id)
            finally:
                if self.recorder and job.protect_token is not None:
                    self.recorder.buffer.release(job.protect_token)
                    job.protect_token = None

    def _wait_until(self, wall_time: float) -> None:
        while not self._stop.is_set() and time.time() < wall_time:
            self._stop.wait(min(1.0, wall_time - time.time()))

    def _cut(self, job: ClipJob) -> None:
        c = self.cfg.clips
        trigger = job.event.trigger_time
        # Warten, bis der Chat-Nachlauf vorbei ist (für Peak-Score + Chat-Auswertung)
        chat_start, chat_end = self._chat_range(job)
        self._wait_until(max(chat_end, trigger + 3))
        job.score = max(job.score, self.detector.peak_score_between(trigger, chat_end))
        job.event.peak_score = job.score
        job.summary = self.chat_signal.summary(chat_start, chat_end, top_n=c.top_messages)

        if self.test_mode or self.recorder is None:
            job.state = JobState.TEST
            log.info(
                "[TEST] Highlight #%d um %s – Peak-Score %.2f | Chat: %s",
                job.event.id, dt.datetime.fromtimestamp(trigger).strftime("%H:%M:%S"), job.score,
                job.top_chat or "-",
            )
            return

        seg_len = self.cfg.recording.segment_seconds
        job.state = JobState.WAITING
        covered = self.recorder.buffer.wait_until_covered(job.clip_end, timeout=seg_len * 3 + 30, stop=self._stop)
        if not covered:
            log.warning("Highlight #%d: Aufnahme reicht nicht bis zum Clip-Ende – schneide, was da ist.", job.event.id)
        segments = self.recorder.buffer.segments_for(job.clip_start, job.clip_end)
        if not segments:
            raise FFmpegError("Für diesen Moment ist keine Aufnahme im Puffer (Stream offline/Werbung?).")

        when = dt.datetime.fromtimestamp(job.moment)
        job.base_name = safe_filename(f"{self.channel}_{when:%Y-%m-%d_%H-%M-%S}_score{job.score:.1f}")
        job.folder = self.cfg.output_path / self.channel / job.base_name
        job.folder.mkdir(parents=True, exist_ok=True)
        raw = job.folder / f"{job.base_name}_raw.mp4"
        job.state = JobState.CUTTING
        log.info("Highlight #%d: schneide %d Segmente …", job.event.id, len(segments))
        real_start, real_end = cut_from_segments(self.ffmpeg, segments, job.clip_start, job.clip_end, raw, c)
        job.files["raw"] = raw
        if real_start > job.clip_start + 1 or real_end < job.clip_end - 1:
            log.warning(
                "Highlight #%d: Clip ist gekürzt (%.0f s statt %.0f s) – Puffer unvollständig.",
                job.event.id, real_end - real_start, job.clip_end - job.clip_start,
            )
        job.moment_offset = max(0.0, job.moment - real_start)
        self._write_chat_files(job)
        job.state = JobState.PROCESSING
        self._proc_queue.put(job)

    # ------------------------------------------------------------ Verarbeiten
    def _processor(self) -> None:
        while True:
            job = self._proc_queue.get()
            if job is None:
                return
            try:
                self._process(job)
            except Exception as exc:
                job.state = JobState.FAILED
                job.error = str(exc)
                log.exception("Verarbeitung von Highlight #%d fehlgeschlagen", job.event.id)

    def _process(self, job: ClipJob) -> None:
        cfg = self.cfg
        assert job.folder is not None
        raw = job.files["raw"]
        base = job.folder / job.base_name
        want_landscape = cfg.clips.format in ("16:9", "both")
        want_shorts = cfg.clips.format in ("9:16", "both")

        if want_landscape:
            landscape = base.with_name(job.base_name + ".mp4")
            raw.replace(landscape)
            job.files["landscape"] = landscape
            source = landscape
        else:
            source = raw
        if want_shorts:
            log.info("Highlight #%d: erstelle 9:16-Version …", job.event.id)
            job.files["shorts"] = transform_clip(
                self.ffmpeg, source, base.with_name(job.base_name + "_shorts.mp4"), cfg.clips, vertical=True,
            )

        transcript_text = ""
        if cfg.subtitles.enabled:
            job.state = JobState.TRANSCRIBING
            try:
                transcript_text = self._subtitles(job, source, want_landscape, want_shorts)
            except Exception as exc:  # Untertitel sind optional – Clip bleibt erhalten
                log.error("Untertitel für Highlight #%d fehlgeschlagen: %s", job.event.id, exc)
                job.error = f"Untertitel: {exc}"

        if not want_landscape:
            raw.unlink(missing_ok=True)  # nur Zwischenprodukt für die 9:16-Version
        job.files.pop("raw", None)

        # Vorschaubild aus dem Moment
        preview = job.preview
        if preview is not None:
            thumb = make_thumbnail(
                self.ffmpeg, preview, base.with_name(job.base_name + "_thumb.jpg"),
                job.moment_offset,
            )
            if thumb:
                job.files["thumbnail"] = thumb

        # YouTube-Texte
        when = dt.datetime.fromtimestamp(job.moment)
        metas = {}
        if want_landscape:
            metas["landscape"] = build_metadata(cfg, self.channel, self.display_name, when, job.summary, transcript_text)
            write_metadata_files(metas["landscape"], base.with_name(job.base_name + "_youtube.txt"), cfg.text_language)
            job.files["youtube_txt"] = base.with_name(job.base_name + "_youtube.txt")
        if want_shorts:
            metas["shorts"] = build_metadata(cfg, self.channel, self.display_name, when, job.summary, transcript_text, shorts=True)
            write_metadata_files(metas["shorts"], base.with_name(job.base_name + "_shorts_youtube.txt"), cfg.text_language)
            job.files["shorts_youtube_txt"] = base.with_name(job.base_name + "_shorts_youtube.txt")

        if cfg.youtube.upload_enabled and cfg.youtube.auto_upload:
            job.state = JobState.UPLOADING
            self.upload(job)
        job.state = JobState.DONE
        log.info("✅ Highlight #%d fertig: %s", job.event.id, job.folder)

    def _subtitles(self, job: ClipJob, source: Path, landscape: bool, shorts: bool) -> str:
        cfg = self.cfg
        if self.transcriber is None:
            self.transcriber = Transcriber(cfg.subtitles)
        log.info("Highlight #%d: transkribiere Audio …", job.event.id)
        tr = self.transcriber.transcribe(source)
        job.language = tr.language
        assert job.folder is not None
        base = job.folder / job.base_name
        cues_l = build_cues(tr.words, cfg.subtitles.max_chars_per_line_landscape)
        write_srt(cues_l, base.with_name(job.base_name + ".srt"))
        job.files["srt"] = base.with_name(job.base_name + ".srt")
        if not tr.words:
            log.info("Highlight #%d: keine Sprache erkannt – keine eingebrannten Untertitel.", job.event.id)
            return ""
        if landscape:
            ass = write_ass(cues_l, base.with_name(job.base_name + ".ass"), cfg.subtitles, vertical=False)
            job.files["landscape_subbed"] = transform_clip(
                self.ffmpeg, source, base.with_name(job.base_name + "_untertitelt.mp4"), cfg.clips, ass_file=ass,
            )
            ass.unlink(missing_ok=True)
        if shorts:
            cues_s = build_cues(tr.words, cfg.subtitles.max_chars_per_line_shorts)
            ass = write_ass(cues_s, base.with_name(job.base_name + "_shorts.ass"), cfg.subtitles, vertical=True)
            job.files["shorts_subbed"] = transform_clip(
                self.ffmpeg, source, base.with_name(job.base_name + "_shorts_untertitelt.mp4"), cfg.clips,
                vertical=True, ass_file=ass,
            )
            ass.unlink(missing_ok=True)
        return tr.text

    def _write_chat_files(self, job: ClipJob) -> None:
        assert job.folder is not None
        s = job.summary
        data = {
            "channel": self.channel,
            "moment": dt.datetime.fromtimestamp(job.moment).isoformat(timespec="seconds"),
            "detected_at": dt.datetime.fromtimestamp(job.event.trigger_time).isoformat(timespec="seconds"),
            "score": round(job.score, 2),
            "threshold": job.event.threshold,
            "stream_delay_seconds": self.cfg.recording.stream_delay_seconds,
            **s,
        }
        (job.folder / f"{job.base_name}_chat.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            f"Kanal: {self.channel}   Moment: {data['moment']}   Score: {data['score']}",
            f"Nachrichten: {s.get('messages', 0)}   Verschiedene Chatter: {s.get('chatters', 0)}",
            "",
            "Häufigste Emotes/Hype-Begriffe (Anzahl User):",
            *[f"  {e['users']:>4}  {e['text']}" for e in s.get("top_emotes", [])],
            "",
            "Häufigste Nachrichten (Anzahl User):",
            *[f"  {m['users']:>4}  {m['text']}" for m in s.get("top_messages", [])],
            "",
            "Häufigste Wörter:",
            *[f"  {w['users']:>4}  {w['text']}" for w in s.get("top_words", [])],
        ]
        (job.folder / f"{job.base_name}_chat.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        job.files["chat_txt"] = job.folder / f"{job.base_name}_chat.txt"

    # ------------------------------------------------------------ Upload
    def upload(self, job: ClipJob) -> list[str]:
        """Lädt die fertigen Videos eines Jobs hoch (privat). Gibt URLs zurück."""
        ok, reason = upload_available(self.cfg.youtube, self.cfg)
        if not ok:
            raise RuntimeError(reason)
        from .youtube import VideoMetadata

        urls = []
        for kind, video_key, meta_key in (
            ("landscape", "landscape_subbed", "youtube_txt"),
            ("landscape", "landscape", "youtube_txt"),
            ("shorts", "shorts_subbed", "shorts_youtube_txt"),
            ("shorts", "shorts", "shorts_youtube_txt"),
        ):
            if kind in job.youtube_ids or video_key not in job.files or meta_key not in job.files:
                continue
            meta = VideoMetadata(**json.loads(job.files[meta_key].with_suffix(".json").read_text(encoding="utf-8")))
            vid = upload_video(job.files[video_key], meta, self.cfg)
            job.youtube_ids[kind] = vid
            urls.append(f"https://youtu.be/{vid}")
        return urls

    # ------------------------------------------------------------ Status
    def status(self) -> dict[str, Any]:
        now = time.time()
        rec = self.recorder
        return {
            "channel": self.channel,
            "running": self.running,
            "test_mode": self.test_mode,
            "uptime": now - self.started_at if self.started_at else 0,
            "chat_status": self.chat.status,
            "chat_messages": self.chat_signal.total_messages,
            "ignored_bot": self.chat_signal.ignored_bot,
            "ignored_spam": self.chat_signal.ignored_spam,
            "warming_up": not self.chat_signal.is_ready(now),
            "warmup_left": max(0.0, self.cfg.detection.warmup_seconds - (now - self.started_at)),
            "recorder_status": rec.status if rec else "aus (Testmodus)",
            "buffered_seconds": rec.buffer.buffered_seconds() if rec else 0.0,
            "highlights": len(self.jobs),
            "cooldown": self.detector.in_cooldown(now),
        }

    def jobs_snapshot(self) -> list[ClipJob]:
        with self._jobs_lock:
            return list(self.jobs)


def check_tools(cfg: AppConfig, test_mode: bool) -> list[str]:
    """Prüft vorab, ob ffmpeg/streamlink verfügbar sind. Gibt Fehlertexte zurück."""
    problems = []
    if test_mode:
        return problems
    try:
        find_ffmpeg(cfg.recording.ffmpeg_path)
    except FileNotFoundError as exc:
        problems.append(str(exc))
    try:
        from .utils import streamlink_command

        streamlink_command(cfg.recording.streamlink_path)
    except FileNotFoundError as exc:
        problems.append(str(exc))
    return problems
