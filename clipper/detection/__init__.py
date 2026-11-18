"""Highlight-Erkennung. Austauschbar über das ``Signal``-Interface."""

from .base import Signal, SignalSample
from .chat_signal import ChatActivitySignal, HypeMatcher, normalize_text
from .detector import DetectorSample, HighlightDetector, HighlightEvent

__all__ = [
    "Signal",
    "SignalSample",
    "ChatActivitySignal",
    "HypeMatcher",
    "normalize_text",
    "HighlightDetector",
    "HighlightEvent",
    "DetectorSample",
]
