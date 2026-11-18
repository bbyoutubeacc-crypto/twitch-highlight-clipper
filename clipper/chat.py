"""Anonymer Twitch-Chat-Client (IRC, justinfan-Login – kein API-Key nötig).

Läuft in einem eigenen Thread, verbindet sich bei Fehlern automatisch neu
(exponentielles Backoff) und reicht jede Chat-Nachricht als ``ChatMessage``
an einen Callback weiter.
"""

from __future__ import annotations

import logging
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import ChatConfig

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ChatMessage:
    timestamp: float          # Empfangszeit (time.time())
    user: str                 # Login-Name, klein geschrieben
    text: str
    display_name: str = ""
    emotes: list[str] = field(default_factory=list)  # Twitch-Emotes laut Tags

    def to_dict(self) -> dict:
        return {"t": round(self.timestamp, 3), "u": self.user, "m": self.text, "e": self.emotes}

    @classmethod
    def from_dict(cls, d: dict) -> "ChatMessage":
        return cls(timestamp=float(d["t"]), user=str(d["u"]), text=str(d["m"]), emotes=list(d.get("e", [])))


# ---------------------------------------------------------------------------
# IRC-Parsing
# ---------------------------------------------------------------------------
_TAG_UNESCAPE = {"\\s": " ", "\\:": ";", "\\\\": "\\", "\\r": "\r", "\\n": "\n"}


def _unescape_tag(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        pair = value[i : i + 2]
        if pair in _TAG_UNESCAPE:
            out.append(_TAG_UNESCAPE[pair])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def parse_irc_line(line: str) -> tuple[dict[str, str], str, str, list[str], str]:
    """Zerlegt eine IRC-Zeile in (tags, prefix, command, params, trailing)."""
    tags: dict[str, str] = {}
    prefix = ""
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for item in raw_tags.split(";"):
            k, _, v = item.partition("=")
            tags[k] = _unescape_tag(v)
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = ""
    if " :" in line:
        line, _, trailing = line.partition(" :")
    elif line.startswith(":"):
        trailing, line = line[1:], ""
    parts = line.split()
    command = parts[0].upper() if parts else ""
    return tags, prefix, command, parts[1:], trailing


def emotes_from_tags(text: str, emote_tag: str) -> list[str]:
    """Liest Emote-Namen über die Positionsangaben im 'emotes'-Tag aus."""
    names: list[str] = []
    if not emote_tag:
        return names
    for group in emote_tag.split("/"):
        _, _, ranges = group.partition(":")
        for rng in ranges.split(","):
            try:
                start, end = (int(x) for x in rng.split("-"))
            except ValueError:
                continue
            # Twitch zählt Unicode-Codepoints – Python-Strings ebenso
            names.append(text[start : end + 1])
    return names


def parse_privmsg(line: str, now: float | None = None) -> ChatMessage | None:
    tags, prefix, command, params, trailing = parse_irc_line(line)
    if command != "PRIVMSG":
        return None
    user = prefix.split("!", 1)[0].lower() if prefix else tags.get("display-name", "").lower()
    text = trailing
    # /me-Nachrichten (ACTION) auspacken
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]
    return ChatMessage(
        timestamp=now if now is not None else time.time(),
        user=user,
        text=text,
        display_name=tags.get("display-name", "") or user,
        emotes=emotes_from_tags(text, tags.get("emotes", "")),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class TwitchChatClient:
    """Liest den Chat eines Kanals in einem Hintergrund-Thread."""

    def __init__(
        self,
        channel: str,
        on_message: Callable[[ChatMessage], None],
        config: ChatConfig | None = None,
    ) -> None:
        self.channel = channel.lower().lstrip("#")
        self.on_message = on_message
        self.cfg = config or ChatConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self.connected = False
        self.status = "getrennt"
        self.messages_received = 0
        self.reconnects = 0

    # ----------------------------------------------------------- Steuerung
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"chat-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread:
            self._thread.join(timeout=5)
        self.connected = False
        self.status = "gestoppt"

    # -------------------------------------------------------------- intern
    def _close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _send(self, line: str) -> None:
        if self._sock is None:
            raise ConnectionError("keine Verbindung")
        self._sock.sendall((line + "\r\n").encode("utf-8"))

    def _connect(self) -> None:
        raw = socket.create_connection((self.cfg.host, self.cfg.port), timeout=15)
        if self.cfg.use_ssl:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self.cfg.host)
        raw.settimeout(1.0)  # kurz, damit stop() schnell greift
        self._sock = raw
        nick = f"justinfan{random.randint(10000, 99999)}"
        self._send("CAP REQ :twitch.tv/tags twitch.tv/commands")
        self._send("PASS SCHMOOPIIE")
        self._send(f"NICK {nick}")
        self._send(f"JOIN #{self.channel}")
        log.info("Verbinde mit Chat von #%s als %s …", self.channel, nick)

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                self.status = "verbinde …"
                self._connect()
                self._read_loop()
                if not self._stop.is_set():
                    log.warning("Chat-Verbindung wurde vom Server beendet.")
            except (OSError, ssl.SSLError, ConnectionError) as exc:
                if not self._stop.is_set():
                    log.warning("Chat-Verbindung fehlgeschlagen: %s", exc)
            except Exception:  # pragma: no cover - unerwartet, aber nie abstürzen
                log.exception("Unerwarteter Fehler im Chat-Client")
            finally:
                was_connected = self.connected
                self.connected = False
                self._close()
            if self._stop.is_set():
                break
            if was_connected:
                delay = 1.0  # stand stabil – schnell neu verbinden
            self.reconnects += 1
            self.status = f"getrennt – neuer Versuch in {delay:.0f} s"
            log.info("Neuer Chat-Verbindungsversuch in %.0f s …", delay)
            self._stop.wait(delay)
            delay = min(delay * 2, self.cfg.reconnect_max_delay)

    def _read_loop(self) -> None:
        buf = b""
        last_data = time.monotonic()
        ping_sent = False
        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(65536) if self._sock else b""
                if not chunk:
                    return  # Server hat geschlossen
            except (socket.timeout, TimeoutError):
                idle = time.monotonic() - last_data
                if idle > self.cfg.idle_timeout and not ping_sent:
                    self._send("PING :tmi.twitch.tv")
                    ping_sent = True
                elif idle > self.cfg.idle_timeout + 30:
                    raise ConnectionError("keine Antwort vom Chat-Server (Timeout)")
                continue
            except ssl.SSLWantReadError:
                continue
            last_data = time.monotonic()
            ping_sent = False
            buf += chunk
            *lines, buf = buf.split(b"\r\n")
            for raw in lines:
                if raw:
                    self._handle_line(raw.decode("utf-8", errors="replace"))

    def _handle_line(self, line: str) -> None:
        if line.startswith("PING"):
            self._send("PONG" + line[4:])
            return
        tags, prefix, command, params, trailing = parse_irc_line(line)
        if command == "PRIVMSG":
            msg = parse_privmsg(line)
            if msg is not None:
                self.messages_received += 1
                try:
                    self.on_message(msg)
                except Exception:  # pragma: no cover
                    log.exception("Fehler bei der Verarbeitung einer Chat-Nachricht")
        elif command == "001":
            log.debug("Am Chat-Server angemeldet.")
        elif command in ("JOIN", "ROOMSTATE") and not self.connected:
            self.connected = True
            self.status = "verbunden"
            log.info("Chat von #%s verbunden.", self.channel)
        elif command == "RECONNECT":
            log.info("Twitch fordert Neuverbindung an (RECONNECT).")
            raise ConnectionError("RECONNECT vom Server")
        elif command == "NOTICE":
            log.warning("Chat-Hinweis von Twitch: %s", trailing)
            if "login" in trailing.lower() or "authentication" in trailing.lower():
                raise ConnectionError(trailing)
