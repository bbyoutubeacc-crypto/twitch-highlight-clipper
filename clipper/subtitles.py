"""Transkription mit faster-whisper und Untertitel-Dateien (.srt / .ass).

* Sprache wird automatisch erkannt (oder fest über ``subtitles.language``).
* Wort-Zeitstempel werden zu gut lesbaren Untertitel-Blöcken gruppiert –
  für Shorts kürzer als für 16:9.
* Die .ass-Datei legt den Stil fest (weiße Schrift, schwarze Kontur).
  PlayRes = 1920x1080 bzw. 1080x1920, d. h. Schriftgrößen sind Pixel bei
  Full-HD; libass skaliert auf die echte Videogröße.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import SubtitleConfig

log = logging.getLogger(__name__)


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Cue:
    start: float
    end: float
    text: str  # Zeilen durch "\n" getrennt


@dataclass
class Transcript:
    language: str
    language_probability: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words).strip()


# Bekannte Whisper-"Halluzinationen" bei Stille/Musik
_HALLUCINATIONS = [
    "untertitel im auftrag des zdf", "untertitelung des zdf", "untertitel von stephanie geiges",
    "untertitel der amara.org-community", "amara.org", "vielen dank fürs zuschauen",
    "danke fürs zuschauen", "thanks for watching", "thank you for watching",
    "please subscribe", "abonniert den kanal", "copyright wdr", "swr 2020", "ard text im auftrag",
]


def _is_hallucination(text: str) -> bool:
    low = text.lower().strip()
    return any(h in low for h in _HALLUCINATIONS)


class Transcriber:
    """Lädt das Whisper-Modell beim ersten Gebrauch und hält es im Speicher."""

    def __init__(self, cfg: SubtitleConfig) -> None:
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError("faster-whisper ist nicht installiert (pip install faster-whisper).") from exc
            log.info("Lade Whisper-Modell '%s' (beim ersten Mal wird es heruntergeladen) …", self.cfg.model)
            try:
                self._model = WhisperModel(self.cfg.model, device=self.cfg.device, compute_type=self.cfg.compute_type)
            except ValueError:
                # z. B. float16 auf CPU nicht verfügbar -> sicherer Fallback
                log.warning("compute_type '%s' nicht unterstützt – verwende int8.", self.cfg.compute_type)
                self._model = WhisperModel(self.cfg.model, device=self.cfg.device, compute_type="int8")
        return self._model

    def transcribe(self, media: Path) -> Transcript:
        with self._lock:  # ein Modell, eine Transkription gleichzeitig
            model = self._load()
            segments, info = model.transcribe(
                str(media),
                language=self.cfg.language or None,
                beam_size=5,
                word_timestamps=True,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
            )
            words: list[Word] = []
            for seg in segments:
                if _is_hallucination(seg.text) or (seg.no_speech_prob > 0.7 and seg.avg_logprob < -1.0):
                    log.debug("Verwerfe vermutliche Halluzination: %r", seg.text)
                    continue
                if seg.words:
                    for w in seg.words:
                        if w.word.strip():
                            words.append(Word(float(w.start), float(w.end), w.word.strip()))
                elif seg.text.strip():
                    words.append(Word(float(seg.start), float(seg.end), seg.text.strip()))
        log.info(
            "Transkription fertig: Sprache=%s (%.0f %%), %d Wörter",
            info.language, info.language_probability * 100, len(words),
        )
        return Transcript(info.language, float(info.language_probability), words)


# ---------------------------------------------------------------------------
# Gruppierung in Untertitel-Blöcke
# ---------------------------------------------------------------------------
def wrap_lines(text: str, max_chars: int) -> list[str]:
    """Bricht Text in Zeilen um (ohne Wörter zu trennen); zwei Zeilen werden
    möglichst gleich lang gemacht."""
    words = text.split()
    if not words:
        return []
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    lines.append(cur)
    if len(lines) == 2:  # zwei Zeilen möglichst gleich lang machen
        all_words = text.split()
        best = None
        for i in range(1, len(all_words)):
            a, b = " ".join(all_words[:i]), " ".join(all_words[i:])
            if len(a) <= max_chars and len(b) <= max_chars:
                diff = abs(len(a) - len(b))
                if best is None or diff < best[0]:
                    best = (diff, [a, b])
        if best:
            lines = best[1]
    return lines


def build_cues(
    words: list[Word],
    max_chars_per_line: int,
    max_lines: int = 2,
    max_duration: float = 5.0,
    max_gap: float = 0.8,
) -> list[Cue]:
    cues: list[Cue] = []
    limit = max_chars_per_line * max_lines
    group: list[Word] = []

    def flush() -> None:
        if not group:
            return
        text = " ".join(w.text for w in group)
        lines = wrap_lines(text, max_chars_per_line)
        # Passt der Block nicht in max_lines, in mehrere Cues aufteilen
        start = group[0].start
        if len(lines) > max_lines:
            # proportionale Zeitaufteilung über die Zeichenanzahl
            total_chars = sum(len(l) for l in lines) or 1
            t = start
            dur = group[-1].end - start
            for i in range(0, len(lines), max_lines):
                part = lines[i : i + max_lines]
                part_dur = dur * sum(len(l) for l in part) / total_chars
                cues.append(Cue(t, t + part_dur, "\n".join(part)))
                t += part_dur
        else:
            cues.append(Cue(start, group[-1].end, "\n".join(lines)))
        group.clear()

    for w in words:
        if group:
            candidate = " ".join(x.text for x in group) + " " + w.text
            too_long = len(candidate) > limit or len(wrap_lines(candidate, max_chars_per_line)) > max_lines
            too_slow = w.end - group[0].start > max_duration
            gap = w.start - group[-1].end > max_gap
            if too_long or too_slow or gap:
                flush()
        group.append(w)
        if re.search(r"[.!?…]$", w.text) and len(" ".join(x.text for x in group)) > limit * 0.4:
            flush()
    flush()

    # Mindestanzeigedauer & keine Überlappung
    for i, c in enumerate(cues):
        c.end = max(c.end, c.start + 0.8)
        if i + 1 < len(cues):
            c.end = max(min(c.end, cues[i + 1].start - 0.02), c.start + 0.1)
            cues[i + 1].start = max(cues[i + 1].start, c.end + 0.02)
    return cues


# ---------------------------------------------------------------------------
# Dateiformate
# ---------------------------------------------------------------------------
def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_srt(cues: list[Cue], path: Path) -> Path:
    blocks = [f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _ass_escape(text: str) -> str:
    text = text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
    return text.replace("\n", "\\N")


def write_ass(cues: list[Cue], path: Path, cfg: SubtitleConfig, vertical: bool) -> Path:
    if vertical:
        res_x, res_y = 1080, 1920
        size, alignment, margin_v, margin_lr = cfg.font_size_shorts, 5, 0, 60
    else:
        res_x, res_y = 1920, 1080
        size, alignment, margin_v, margin_lr = cfg.font_size_landscape, 2, 70, 120
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cfg.font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{cfg.outline},1,{alignment},{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [
        f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Default,,0,0,0,,{_ass_escape(c.text)}"
        for c in cues
    ]
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path
