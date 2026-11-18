"""Gemeinsames Interface für Highlight-Signale.

Ein *Signal* liefert pro Zeitpunkt einen Score in der Einheit
"x-fach über dem Normalwert" (1.0 = normal, 3.0 = dreimal so viel wie
üblich). Dadurch lassen sich verschiedene Signale (Chat, später Audio,
…) gewichtet kombinieren, ohne dass der Detektor wissen muss, woher die
Daten kommen.

Neues Signal ergänzen (z. B. Audio):

    class AudioSignal(Signal):
        name = "audio"
        def feed(self, samples, timestamp): ...   # vom Recorder gefüttert
        def update(self, now): return SignalSample(...)
        def is_ready(self, now): ...

und in ``session.py`` zusätzlich zum Chat-Signal an den
``HighlightDetector`` übergeben (Gewicht in ``detection.signal_weights``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SignalSample:
    time: float               # Zeitpunkt (time.time())
    score: float              # "x-fach über normal" (nach Dämpfung/Bonus)
    value: float = 0.0        # Rohwert fürs Diagramm (z. B. Nachrichten/s)
    baseline: float = 0.0     # Normalwert in derselben Einheit wie value
    details: dict[str, Any] = field(default_factory=dict)


class Signal(ABC):
    """Basisklasse aller Signale."""

    #: eindeutiger Name, gleichzeitig Schlüssel in ``signal_weights``
    name: str = "signal"

    @abstractmethod
    def update(self, now: float) -> SignalSample:
        """Berechnet den aktuellen Score. Wird ca. einmal pro Sekunde aufgerufen."""

    def is_ready(self, now: float) -> bool:
        """False während der Aufwärmphase (Normalwert noch unbekannt)."""
        return True

    def summary(self, start: float, end: float) -> dict[str, Any]:
        """Kontext für einen Clip-Zeitraum (z. B. häufigste Chat-Nachrichten)."""
        return {}

    def reset(self) -> None:
        """Alle gesammelten Daten verwerfen."""
