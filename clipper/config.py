"""Laden und Prüfen der config.yaml.

Die Konfiguration wird in typisierte Dataclasses übersetzt. Fehlende Werte
werden mit den Standardwerten aus den Dataclasses aufgefüllt, unbekannte
Schlüssel erzeugen eine Warnung (Tippfehler fallen so schnell auf).
"""

from __future__ import annotations

import copy
import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Programmordner (eine Ebene über dem Paket "clipper")
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.yaml"


@dataclass
class ChatConfig:
    host: str = "irc.chat.twitch.tv"
    port: int = 6697
    use_ssl: bool = True
    reconnect_max_delay: float = 60.0
    idle_timeout: float = 300.0


@dataclass
class RecordingConfig:
    quality: str = "best"
    segment_seconds: float = 10.0
    buffer_seconds: float = 120.0
    temp_dir: str = "buffer"
    stream_delay_seconds: float = 6.0
    offline_retry_seconds: float = 30.0
    ffmpeg_path: str = ""
    streamlink_path: str = ""
    streamlink_args: list[str] = field(default_factory=list)


@dataclass
class DetectionConfig:
    threshold: float = 3.0
    window_seconds: float = 5.0
    baseline_seconds: float = 180.0
    warmup_seconds: float = 60.0
    min_baseline_rate: float = 0.3
    min_messages: int = 6
    cooldown_seconds: float = 60.0
    hype_multiplier: float = 0.6
    spam_window_seconds: float = 30.0
    max_messages_per_user: int = 3
    ignored_users: list[str] = field(default_factory=lambda: ["nightbot", "streamelements"])
    hype_terms: dict[str, float] = field(default_factory=lambda: {"KEKW": 1.0, "POG": 1.0, "clip": 1.5})
    signal_weights: dict[str, float] = field(default_factory=lambda: {"chat": 1.0})


@dataclass
class ClipConfig:
    pre_seconds: float = 30.0
    post_seconds: float = 15.0
    format: str = "16:9"  # "16:9", "9:16" oder "both"
    video_preset: str = "veryfast"
    crf: int = 20
    audio_bitrate: str = "160k"
    top_messages: int = 15


@dataclass
class SubtitleConfig:
    enabled: bool = True
    model: str = "small"
    device: str = "auto"
    compute_type: str = "auto"
    language: str = ""
    font: str = "Arial"
    font_size_landscape: int = 54
    font_size_shorts: int = 80
    outline: int = 4
    max_chars_per_line_landscape: int = 42
    max_chars_per_line_shorts: int = 22


@dataclass
class YouTubeConfig:
    upload_enabled: bool = False
    auto_upload: bool = False
    client_secrets_file: str = "client_secret.json"
    token_file: str = "youtube_token.json"
    privacy_status: str = "private"
    category_id: str = "20"
    extra_tags: list[str] = field(default_factory=lambda: ["twitch", "highlights"])


@dataclass
class UIConfig:
    host: str = "127.0.0.1"
    port: int = 7860
    chart_seconds: float = 180.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/clipper.log"
    save_chat_in_test_mode: bool = True


@dataclass
class AppConfig:
    output_dir: str = "clips"
    text_language: str = "de"
    chat: ChatConfig = field(default_factory=ChatConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    clips: ClipConfig = field(default_factory=ClipConfig)
    subtitles: SubtitleConfig = field(default_factory=SubtitleConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    # ------------------------------------------------------------------ Pfade
    def resolve_path(self, value: str) -> Path:
        """Relative Pfade beziehen sich auf den Programmordner."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def output_path(self) -> Path:
        return self.resolve_path(self.output_dir)

    @property
    def buffer_path(self) -> Path:
        return self.resolve_path(self.recording.temp_dir)

    def copy(self) -> "AppConfig":
        return copy.deepcopy(self)

    # ------------------------------------------------------------ Validierung
    def validate(self) -> list[str]:
        """Prüft die Werte, korrigiert Unsinniges und gibt Warnungen zurück."""
        warnings: list[str] = []
        r, c, d = self.recording, self.clips, self.detection

        if c.format not in ("16:9", "9:16", "both"):
            warnings.append(f"clips.format '{c.format}' ist ungültig – verwende '16:9'.")
            c.format = "16:9"
        if r.segment_seconds < 2:
            warnings.append("recording.segment_seconds < 2 – setze auf 2.")
            r.segment_seconds = 2.0
        if c.pre_seconds < 0 or c.post_seconds < 0:
            warnings.append("Clip-Längen dürfen nicht negativ sein – setze auf 0.")
            c.pre_seconds, c.post_seconds = max(0.0, c.pre_seconds), max(0.0, c.post_seconds)
        if c.pre_seconds + c.post_seconds < 3:
            warnings.append("Clip wäre kürzer als 3 s – setze post_seconds hoch.")
            c.post_seconds = 3.0 - c.pre_seconds

        # Der Puffer muss den ganzen Clip enthalten können – inkl. Delay,
        # dem noch offenen Segment und etwas Reserve.
        needed = c.pre_seconds + c.post_seconds + max(0.0, r.stream_delay_seconds) + 3 * r.segment_seconds
        if r.buffer_seconds < needed:
            warnings.append(
                f"recording.buffer_seconds ({r.buffer_seconds:.0f}) ist zu klein für "
                f"{c.pre_seconds:.0f}+{c.post_seconds:.0f} s Clips – erhöhe auf {needed:.0f}."
            )
            r.buffer_seconds = needed

        if d.threshold <= 0:
            warnings.append("detection.threshold muss > 0 sein – setze auf 3.0.")
            d.threshold = 3.0
        if d.window_seconds < 1:
            d.window_seconds = 1.0
        if d.baseline_seconds < d.window_seconds * 4:
            warnings.append("detection.baseline_seconds ist sehr kurz – setze auf 4x window_seconds.")
            d.baseline_seconds = d.window_seconds * 4
        if self.text_language not in ("de", "en"):
            warnings.append(f"text_language '{self.text_language}' unbekannt – verwende 'de'.")
            self.text_language = "de"
        return warnings


# ---------------------------------------------------------------------------
def _build(cls: type, data: dict[str, Any] | None, prefix: str = "") -> Any:
    """Baut rekursiv eine Dataclass aus einem Dict (mit Typ-Umwandlung)."""
    data = data or {}
    if not isinstance(data, dict):
        log.warning("Abschnitt '%s' in config.yaml ist kein Block – wird ignoriert.", prefix or "root")
        data = {}
    kwargs: dict[str, Any] = {}
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in fields:
            log.warning("Unbekannter Konfigurationsschlüssel '%s%s' – wird ignoriert.", prefix, key)
            continue
        f = fields[key]
        default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default  # type: ignore[misc]
        if dataclasses.is_dataclass(default):
            kwargs[key] = _build(type(default), value, prefix=f"{prefix}{key}.")
        else:
            kwargs[key] = _coerce(value, default, f"{prefix}{key}")
    return cls(**kwargs)


def _coerce(value: Any, default: Any, name: str) -> Any:
    if value is None:
        return default
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "ja", "on")
            return bool(value)
        if isinstance(default, int) and not isinstance(default, bool):
            return int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, str):
            return str(value)
        if isinstance(default, list):
            return [str(v) for v in value] if isinstance(value, list) else [str(value)]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise TypeError("erwartet Schlüssel/Wert-Paare")
            # Werte in Dicts sind bei uns immer Zahlen (Gewichte)
            return {str(k): float(v if v is not None else 1.0) for k, v in value.items()}
    except (TypeError, ValueError) as exc:
        log.warning("Ungültiger Wert für '%s' (%r): %s – verwende Standard %r.", name, value, exc, default)
        return default
    return value


def load_config(path: str | Path | None = None) -> AppConfig:
    """Lädt die Konfiguration; fehlt die Datei, werden Standardwerte genutzt."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"config.yaml ist fehlerhaft: {exc}") from exc
    else:
        log.warning("Keine Konfiguration unter %s gefunden – verwende Standardwerte.", cfg_path)
    cfg: AppConfig = _build(AppConfig, data)
    for w in cfg.validate():
        log.warning(w)
    return cfg
