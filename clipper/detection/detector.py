"""Kombiniert beliebig viele Signale zu einer Highlight-Entscheidung.

* Gesamt-Score = gewichteter Mittelwert der Scores aller *bereiten* Signale
* Highlight, sobald der Gesamt-Score die Schwelle erreicht
* Danach Cooldown, damit derselbe Moment nicht mehrfach geclippt wird
* Verlauf (für das Live-Diagramm) wird mitgeschrieben
"""

from __future__ import annotations

import collections
import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from .base import Signal, SignalSample

log = logging.getLogger(__name__)
_ids = itertools.count(1)


@dataclass
class DetectorSample:
    time: float
    score: float
    threshold: float
    ready: bool
    signals: dict[str, SignalSample] = field(default_factory=dict)


@dataclass
class HighlightEvent:
    id: int
    trigger_time: float           # Zeitpunkt der Erkennung (Chat-Zeit)
    score: float                  # Score beim Auslösen
    threshold: float
    signals: dict[str, SignalSample] = field(default_factory=dict)
    peak_score: float = 0.0       # wird nachträglich auf den Höchstwert gesetzt


class HighlightDetector:
    def __init__(
        self,
        signals: list[Signal],
        weights: dict[str, float] | None = None,
        threshold: float = 3.0,
        cooldown_seconds: float = 60.0,
        on_highlight: Callable[[HighlightEvent], None] | None = None,
        history_seconds: float = 900.0,
    ) -> None:
        self.signals = list(signals)
        self.weights = dict(weights or {})
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.on_highlight = on_highlight
        self._lock = threading.Lock()
        self._history: collections.deque[DetectorSample] = collections.deque()
        self._history_seconds = history_seconds
        self._last_trigger: float | None = None
        self.highlights: list[HighlightEvent] = []

    # ----------------------------------------------------------- Einstellungen
    def set_threshold(self, threshold: float) -> None:
        self.threshold = max(0.1, float(threshold))

    def set_cooldown(self, seconds: float) -> None:
        self.cooldown_seconds = max(0.0, float(seconds))

    def signal(self, name: str) -> Signal | None:
        return next((s for s in self.signals if s.name == name), None)

    # ------------------------------------------------------------- Auswertung
    def tick(self, now: float) -> DetectorSample:
        """Einmal pro Sekunde aufrufen."""
        samples: dict[str, SignalSample] = {}
        total = weight_sum = 0.0
        ready_any = False
        for sig in self.signals:
            try:
                sample = sig.update(now)
            except Exception:  # ein defektes Signal darf nicht alles stoppen
                log.exception("Signal '%s' ist fehlgeschlagen", sig.name)
                continue
            samples[sig.name] = sample
            if sig.is_ready(now):
                w = self.weights.get(sig.name, 1.0)
                if w > 0:
                    total += w * sample.score
                    weight_sum += w
                    ready_any = True
        score = total / weight_sum if weight_sum > 0 else 0.0
        result = DetectorSample(time=now, score=score, threshold=self.threshold, ready=ready_any, signals=samples)

        event: HighlightEvent | None = None
        with self._lock:
            self._history.append(result)
            while self._history and now - self._history[0].time > self._history_seconds:
                self._history.popleft()
            in_cooldown = self._last_trigger is not None and now - self._last_trigger < self.cooldown_seconds
            if ready_any and not in_cooldown and score >= self.threshold:
                self._last_trigger = now
                event = HighlightEvent(
                    id=next(_ids), trigger_time=now, score=score, threshold=self.threshold,
                    signals=samples, peak_score=score,
                )
                self.highlights.append(event)
        if event is not None:
            log.info("🔥 Highlight #%d erkannt – Score %.2f (Schwelle %.2f)", event.id, score, self.threshold)
            if self.on_highlight:
                try:
                    self.on_highlight(event)
                except Exception:
                    log.exception("Fehler im Highlight-Callback")
        return result

    # --------------------------------------------------------------- Abfragen
    def history(self, seconds: float | None = None) -> list[DetectorSample]:
        with self._lock:
            items = list(self._history)
        if seconds is None or not items:
            return items
        limit = items[-1].time - seconds
        return [s for s in items if s.time >= limit]

    def peak_score_between(self, start: float, end: float) -> float:
        with self._lock:
            scores = [s.score for s in self._history if start <= s.time <= end]
        return max(scores, default=0.0)

    def in_cooldown(self, now: float) -> bool:
        return self._last_trigger is not None and now - self._last_trigger < self.cooldown_seconds
