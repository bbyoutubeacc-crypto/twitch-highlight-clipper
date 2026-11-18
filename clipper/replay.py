"""Gespeicherten Chat (JSONL aus dem Testmodus) offline erneut auswerten.

Damit lassen sich Schwelle, Fenster, Hype-Gewichte usw. in Sekunden
ausprobieren, ohne auf den nächsten Livestream warten zu müssen:

    python app.py --replay logs/chat_kanal_2026-01-01_20-00-00.jsonl --threshold 2.5
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .chat import ChatMessage
from .config import AppConfig
from .detection import ChatActivitySignal, HighlightDetector, HighlightEvent

log = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    highlights: list[tuple[HighlightEvent, dict]]
    messages: int
    duration: float
    max_score: float


def load_chat_log(path: Path) -> list[ChatMessage]:
    msgs = []
    with path.open("r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(ChatMessage.from_dict(json.loads(line)))
            except (ValueError, KeyError) as exc:
                log.warning("Zeile %d in %s übersprungen: %s", n, path.name, exc)
    msgs.sort(key=lambda m: m.timestamp)
    return msgs


def replay(messages: list[ChatMessage], cfg: AppConfig) -> ReplayResult:
    if not messages:
        return ReplayResult([], 0, 0.0, 0.0)
    # Komplette Historie behalten, damit Auswertungen am Ende möglich sind
    keep = messages[-1].timestamp - messages[0].timestamp + 3600
    signal = ChatActivitySignal(cfg.detection, history_seconds=keep)
    events: list[HighlightEvent] = []
    detector = HighlightDetector(
        [signal], cfg.detection.signal_weights, cfg.detection.threshold,
        cfg.detection.cooldown_seconds, on_highlight=events.append, history_seconds=keep,
    )
    start = messages[0].timestamp
    end = messages[-1].timestamp + cfg.clips.post_seconds + 2
    signal.mark_started(start)
    t = float(int(start)) + 1.0
    i = 0
    max_score = 0.0
    while t <= end:
        while i < len(messages) and messages[i].timestamp < t:
            signal.add_message(messages[i])
            i += 1
        sample = detector.tick(t)
        max_score = max(max_score, sample.score)
        t += 1.0

    results = []
    post = cfg.clips.post_seconds
    pre = cfg.clips.pre_seconds
    for ev in events:
        ev.peak_score = detector.peak_score_between(ev.trigger_time, ev.trigger_time + post)
        summary = signal.summary(ev.trigger_time - pre, ev.trigger_time + post, top_n=cfg.clips.top_messages)
        results.append((ev, summary))
    return ReplayResult(results, len(messages), messages[-1].timestamp - start, max_score)


def print_replay(path: Path, cfg: AppConfig) -> ReplayResult:
    msgs = load_chat_log(path)
    res = replay(msgs, cfg)
    d = cfg.detection
    print(f"\nReplay: {path.name}")
    print(f"  {res.messages} Nachrichten über {res.duration / 60:.1f} Minuten")
    print(f"  Schwelle {d.threshold:.2f} | Fenster {d.window_seconds:.0f}s | Normalwert {d.baseline_seconds:.0f}s"
          f" | Cooldown {d.cooldown_seconds:.0f}s | höchster Score {res.max_score:.2f}")
    print(f"  => {len(res.highlights)} Highlights\n")
    for ev, summary in res.highlights:
        when = dt.datetime.fromtimestamp(ev.trigger_time).strftime("%H:%M:%S")
        emotes = ", ".join(f"{e['text']}({e['users']})" for e in summary.get("top_emotes", [])[:4]) or "-"
        top = summary.get("top_messages", [{}])[0].get("text", "-") if summary.get("top_messages") else "-"
        print(f"  #{ev.id:<3} {when}  Score {ev.score:5.2f}  Peak {ev.peak_score:5.2f}  | {emotes} | „{top[:50]}“")
    if not res.highlights and res.max_score > 0:
        print(f"  Tipp: Der höchste Score war {res.max_score:.2f} – Schwelle darunter setzen, um mehr zu finden.")
    print()
    return res
