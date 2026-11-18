"""Chat-Aktivität als Highlight-Signal.

Idee: Wir zählen gefilterte Nachrichten pro Sekunde und vergleichen die
Rate der letzten paar Sekunden mit dem gleitenden Durchschnitt der letzten
Minuten. Das Verhältnis ("x-fach über normal") ist unabhängig von der
Kanalgröße – 3 statt 1 Nachricht/s bei einem kleinen Kanal ist genauso
auffällig wie 300 statt 100 bei einem großen.

Zusätzlich:
* Hype-Begriffe/Emotes erhöhen den Score (Gewichte aus der config.yaml).
* Spam-Schutz: identische Nachrichten desselben Users zählen nur einmal,
  pro User zählen höchstens N Nachrichten im kurzen Fenster.
* Bots werden komplett ignoriert.
"""

from __future__ import annotations

import collections
import math
import re
import threading
from typing import Any

from ..chat import ChatMessage
from ..config import DetectionConfig
from .base import Signal, SignalSample

# Unsichtbare Zeichen, die Chat-Tools anhängen, um Twitchs "doppelte
# Nachricht"-Sperre zu umgehen (z. B. 7TV/Chatterino).
_INVISIBLE_RE = re.compile("[\U000e0000\u200b-\u200f\u2060\ufeff\u034f]")
_WS_RE = re.compile(r"\s+")
_TOKEN_STRIP = "!?.,:;\"'()[]{}<>"

_STOPWORDS = {
    # deutsch
    "der", "die", "das", "und", "ist", "ich", "du", "er", "sie", "es", "wir", "ihr", "ein", "eine",
    "nicht", "was", "wie", "so", "ja", "nein", "auf", "mit", "zu", "in", "den", "dem", "des", "im",
    "an", "am", "auch", "noch", "mal", "hat", "habe", "bin", "bist", "war", "wenn", "dann", "aber",
    "oder", "für", "von", "da", "hier", "man", "mir", "mich", "dich", "dir", "doch", "nur",
    # englisch
    "the", "a", "an", "and", "is", "i", "you", "he", "she", "it", "we", "they", "to", "of", "in",
    "on", "for", "that", "this", "with", "be", "are", "was", "my", "me", "so", "just", "what", "at",
    "but", "or", "not", "do", "no", "yes", "can", "have", "has", "its", "it's", "im", "i'm", "he's",
}


def normalize_text(text: str) -> str:
    """Vereinheitlicht eine Nachricht: unsichtbare Zeichen raus, Leerraum
    zusammenfassen und direkt wiederholte Wörter zusammenziehen
    ("KEKW KEKW KEKW" -> "KEKW")."""
    text = _INVISIBLE_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    out: list[str] = []
    for tok in text.split(" "):
        if not out or out[-1].lower() != tok.lower():
            out.append(tok)
    return " ".join(out)


class HypeMatcher:
    """Findet Hype-Begriffe in Nachrichten und liefert deren Gewicht."""

    def __init__(self, terms: dict[str, float]) -> None:
        self.words: dict[str, float] = {}      # ganze Wörter (lowercase)
        self.phrases: dict[str, float] = {}    # Teilstrings mit Leerzeichen
        for term, weight in (terms or {}).items():
            t = str(term).strip().lower()
            if not t:
                continue
            if " " in t:
                self.phrases[t] = float(weight)
            else:
                self.words[t] = float(weight)

    def match(self, text: str) -> tuple[float, list[str]]:
        """Gibt (Gewicht, gefundene Begriffe) zurück. Gewicht = höchster Treffer,
        damit "KEKW KEKW KEKW" nicht dreifach zählt. Maximal 2.0."""
        lower = text.lower()
        found: list[str] = []
        best = 0.0
        for tok in lower.split():
            stripped = tok.strip(_TOKEN_STRIP) or tok
            for cand in (tok, stripped):
                w = self.words.get(cand)
                if w is not None:
                    found.append(cand)
                    best = max(best, w)
                    break
        padded = f" {lower} "
        for phrase, w in self.phrases.items():
            if phrase in padded:
                found.append(phrase)
                best = max(best, w)
        return min(best, 2.0), found

    def is_term(self, token: str) -> bool:
        t = token.lower()
        return t in self.words or t.strip(_TOKEN_STRIP) in self.words


class _Bucket:
    __slots__ = ("raw", "count", "hype")

    def __init__(self) -> None:
        self.raw = 0       # alle Nachrichten (ohne Bots)
        self.count = 0     # gezählte Nachrichten (nach Spam-Filter)
        self.hype = 0.0    # Summe der Hype-Gewichte der gezählten Nachrichten


class ChatActivitySignal(Signal):
    name = "chat"

    def __init__(self, config: DetectionConfig, history_seconds: float = 900.0) -> None:
        self.cfg = config
        self._lock = threading.Lock()
        self._history_seconds = max(history_seconds, config.baseline_seconds + 60)
        self._buckets: dict[int, _Bucket] = {}
        self._messages: collections.deque[tuple[float, str, str, list[str], bool]] = collections.deque()
        self._last_seen: dict[tuple[str, str], float] = {}
        self._user_times: dict[str, collections.deque[float]] = {}
        self._started: float | None = None
        self.apply_config(config)
        self.total_messages = 0
        self.ignored_bot = 0
        self.ignored_spam = 0

    # ------------------------------------------------------------ Konfig
    def apply_config(self, config: DetectionConfig) -> None:
        """Übernimmt geänderte Einstellungen (auch während des Laufs)."""
        with self._lock:
            self.cfg = config
            self._ignored = {u.lower().lstrip("@") for u in config.ignored_users}
            self.hype = HypeMatcher(config.hype_terms)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._messages.clear()
            self._last_seen.clear()
            self._user_times.clear()
            self._started = None
            self.total_messages = self.ignored_bot = self.ignored_spam = 0

    # ----------------------------------------------------------- Eingang
    def add_message(self, msg: ChatMessage) -> None:
        with self._lock:
            if self._started is None:
                self._started = msg.timestamp
            self.total_messages += 1
            user = msg.user.lower()
            if user in self._ignored:
                self.ignored_bot += 1
                return
            norm = normalize_text(msg.text)
            if not norm:
                return
            sec = int(msg.timestamp)
            bucket = self._buckets.get(sec)
            if bucket is None:
                bucket = self._buckets[sec] = _Bucket()
            bucket.raw += 1

            counted = self._passes_spam_filter(user, norm, msg.timestamp)
            if counted:
                weight, _ = self.hype.match(norm)
                bucket.count += 1
                bucket.hype += weight
            else:
                self.ignored_spam += 1
            self._messages.append((msg.timestamp, user, norm, list(msg.emotes), counted))
            self._prune(msg.timestamp)

    def _passes_spam_filter(self, user: str, norm: str, ts: float) -> bool:
        key = (user, norm.lower())
        last = self._last_seen.get(key)
        self._last_seen[key] = ts
        if last is not None and ts - last < self.cfg.spam_window_seconds:
            return False
        times = self._user_times.get(user)
        if times is None:
            times = self._user_times[user] = collections.deque()
        while times and ts - times[0] > self.cfg.window_seconds:
            times.popleft()
        if len(times) >= self.cfg.max_messages_per_user:
            return False
        times.append(ts)
        return True

    def _prune(self, now: float) -> None:
        limit = now - self._history_seconds
        while self._messages and self._messages[0][0] < limit:
            self._messages.popleft()
        if len(self._buckets) > self._history_seconds + 120:
            for sec in [s for s in self._buckets if s < limit]:
                del self._buckets[sec]
        if len(self._last_seen) > 50_000:
            spam_limit = now - self.cfg.spam_window_seconds
            self._last_seen = {k: t for k, t in self._last_seen.items() if t >= spam_limit}
        if len(self._user_times) > 20_000:
            self._user_times = {u: d for u, d in self._user_times.items() if d and now - d[-1] < 60}

    # ----------------------------------------------------------- Auswertung
    def is_ready(self, now: float) -> bool:
        with self._lock:
            return self._started is not None and now - self._started >= self.cfg.warmup_seconds

    def mark_started(self, now: float) -> None:
        """Startzeitpunkt setzen (auch wenn noch keine Nachricht kam)."""
        with self._lock:
            if self._started is None:
                self._started = now

    def update(self, now: float) -> SignalSample:
        cfg = self.cfg
        with self._lock:
            started = self._started if self._started is not None else now
            cur = int(math.floor(now))  # laufende Sekunde ist noch unvollständig
            w = max(1, int(round(cfg.window_seconds)))
            win_secs = range(cur - w, cur)
            count = 0
            hype = 0.0
            for s in win_secs:
                b = self._buckets.get(s)
                if b:
                    count += b.count
                    hype += b.hype
            last = self._buckets.get(cur - 1)
            raw_last = last.raw if last else 0

            # Normalwert: Durchschnitt VOR dem kurzen Fenster
            base_end = cur - w
            base_start = max(base_end - int(cfg.baseline_seconds), int(math.floor(started)))
            base_len = base_end - base_start
            base_count = 0
            if base_len > 0:
                for s in range(base_start, base_end):
                    b = self._buckets.get(s)
                    if b:
                        base_count += b.count
                baseline = base_count / base_len
            else:
                baseline = 0.0

        rate = count / w
        eff_base = max(baseline, cfg.min_baseline_rate)
        ratio = rate / eff_base if eff_base > 0 else 0.0
        hype_share = hype / count if count else 0.0
        score = ratio * (1.0 + cfg.hype_multiplier * hype_share)
        # Zu wenige Nachrichten -> Score sanft dämpfen (verhindert Fehlalarme
        # in sehr ruhigen Chats, ohne das Diagramm abrupt springen zu lassen).
        if cfg.min_messages > 0 and count < cfg.min_messages:
            score *= count / cfg.min_messages
        return SignalSample(
            time=now,
            score=score,
            value=float(raw_last),
            baseline=baseline,
            details={
                "rate": rate,
                "ratio": ratio,
                "hype_share": hype_share,
                "window_count": count,
            },
        )

    # ------------------------------------------------------------- Kontext
    def summary(self, start: float, end: float, top_n: int = 15) -> dict[str, Any]:
        """Häufigste Nachrichten und Emotes im Zeitraum [start, end]."""
        with self._lock:
            msgs = [m for m in self._messages if start <= m[0] <= end]
        if not msgs:
            return {"messages": 0, "chatters": 0, "top_messages": [], "top_emotes": [], "top_words": []}

        msg_users: dict[str, set[str]] = collections.defaultdict(set)
        msg_display: dict[str, str] = {}
        token_users: dict[str, set[str]] = collections.defaultdict(set)
        token_display: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
        emote_names: set[str] = set()
        for _, user, norm, emotes, _counted in msgs:
            key = norm.lower()
            msg_users[key].add(user)
            msg_display.setdefault(key, norm)
            emote_names.update(e.lower() for e in emotes)
            for tok in set(norm.split()):
                clean = tok.strip(_TOKEN_STRIP) if tok not in ("?", "???", "!clip") else tok
                if not clean:
                    continue
                low = clean.lower()
                token_users[low].add(user)
                token_display[low][clean] += 1

        # Nach Anzahl unterschiedlicher User sortieren (robust gegen Spam)
        ranked = sorted(msg_users.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        # Nachrichten, die nur eine Person geschrieben hat, helfen beim Titel
        # kaum – sie werden nur gezeigt, wenn es sonst (fast) nichts gibt.
        repeated = [kv for kv in ranked if len(kv[1]) >= 2]
        top_messages = (repeated if len(repeated) >= 3 else ranked)[:top_n]
        emotes, words = [], []
        for low, users in sorted(token_users.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            display = token_display[low].most_common(1)[0][0]
            entry = {"text": display, "users": len(users)}
            if low in emote_names or self.hype.is_term(low) or _looks_like_emote(display):
                emotes.append(entry)
            elif low not in _STOPWORDS and len(low) > 2:
                words.append(entry)
        return {
            "messages": len(msgs),
            "chatters": len({m[1] for m in msgs}),
            "top_messages": [{"text": msg_display[k], "users": len(u)} for k, u in top_messages],
            "top_emotes": emotes[:top_n],
            "top_words": words[:top_n],
        }


def _looks_like_emote(token: str) -> bool:
    """Heuristik für BTTV/7TV-Emotes: CamelCase oder GROSS mit Zahlen."""
    if len(token) < 3 or not token.isalnum():
        return False
    has_lower_then_upper = re.search(r"[a-z][A-Z]", token) is not None
    return has_lower_then_upper or (token.isupper() and any(c.isdigit() for c in token))

