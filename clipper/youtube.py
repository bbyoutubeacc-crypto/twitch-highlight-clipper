"""YouTube-Vorbereitung: Titel, Beschreibung, Tags – und optionaler Upload.

Der Upload nutzt die YouTube Data API v3 mit OAuth (Desktop-App). Ohne
``client_secret.json`` oder ohne installierte Google-Bibliotheken
funktioniert alles andere ganz normal weiter.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig, YouTubeConfig
from .utils import channel_url

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

FUNNY = {"kekw", "lul", "lulw", "omegalul", "icant", "haha", "xd", "lmao", "lol", "😂", "🤣"}
HYPE = {"pog", "poggers", "pogchamp", "pogu", "lets go", "letsgo", "let's go", "w", "ww", "www", "ez", "clap", "🔥"}
SHOCK = {"monkas", "monkaw", "wtf", "no way", "omg", "???", "?", "😱", "wait"}
CLIP = {"clip", "!clip", "clip it", "clip that"}

TEXTS = {
    "de": {
        "funny": ["{s} kann nicht mehr 😂", "Chat stirbt vor Lachen 😂 | {s}", "Dieser Moment von {s} ist zu gut 😂"],
        "hype": ["{s} mit einem unglaublichen Moment 🔥", "Chat rastet komplett aus 🔥 | {s}", "Was für ein Play von {s}! 🔥"],
        "shock": ["{s}: Was ist hier gerade passiert?! 😱", "Damit hat {s} nicht gerechnet 😱", "Chat ist sprachlos 😱 | {s}"],
        "clip": ["Chat schreit nach einem Clip! | {s}", "Dieser {s}-Moment MUSSTE geclippt werden"],
        "generic": ["{s} – Highlight vom {d}", "Bester Moment aus dem Stream von {s}"],
        "quote": "„{q}“ – {s}",
        "desc": (
            "{title}\n\n"
            "Highlight aus dem Twitch-Livestream von {s} vom {d} um {t} Uhr.\n\n"
            "🎥 Alle Credits gehen an {s}!\n"
            "👉 Folgt {s} auf Twitch: {url}\n\n"
            "{chat}"
            "Dieser Clip wurde aus dem öffentlichen Livestream erstellt. "
            "Alle Rechte liegen beim Streamer.\n\n"
            "{hashtags}"
        ),
        "chat": "💬 Chat-Reaktion: {x}\n\n",
    },
    "en": {
        "funny": ["{s} can't stop laughing 😂", "Chat is dying 😂 | {s}", "This {s} moment is too good 😂"],
        "hype": ["{s} with an insane moment 🔥", "Chat goes absolutely wild 🔥 | {s}", "What a play by {s}! 🔥"],
        "shock": ["{s}: What just happened?! 😱", "{s} did NOT see that coming 😱", "Chat is speechless 😱 | {s}"],
        "clip": ["Chat begged for a clip! | {s}", "This {s} moment HAD to be clipped"],
        "generic": ["{s} – stream highlight from {d}", "Best moment from {s}'s stream"],
        "quote": "\"{q}\" – {s}",
        "desc": (
            "{title}\n\n"
            "Highlight from {s}'s Twitch livestream on {d} at {t}.\n\n"
            "🎥 All credit goes to {s}!\n"
            "👉 Follow {s} on Twitch: {url}\n\n"
            "{chat}"
            "This clip was made from the public livestream. All rights belong to the streamer.\n\n"
            "{hashtags}"
        ),
        "chat": "💬 Chat reaction: {x}\n\n",
    },
}


@dataclass
class VideoMetadata:
    title: str
    title_alternatives: list[str]
    description: str
    tags: list[str]
    shorts: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _mood(summary: dict) -> str:
    """Bestimmt anhand der Chat-Reaktion die Stimmung des Moments."""
    scores = {"funny": 0, "hype": 0, "shock": 0, "clip": 0}
    for entry in summary.get("top_emotes", []) + summary.get("top_messages", []):
        low = entry["text"].lower()
        for mood, terms in (("funny", FUNNY), ("hype", HYPE), ("shock", SHOCK), ("clip", CLIP)):
            if low in terms or any(f" {t} " in f" {low} " for t in terms if " " in t or len(t) > 3):
                scores[mood] += entry.get("users", 1)
    # "clip" allein ist eher Bonus – nur wählen, wenn sonst nichts dominiert
    best = max(("funny", "hype", "shock"), key=lambda k: scores[k])
    if scores[best] == 0:
        return "clip" if scores["clip"] else "generic"
    return best


def _good_quote(text: str) -> str | None:
    text = text.strip().strip("\"'„“")
    words = text.split()
    if 3 <= len(words) <= 12 and 12 <= len(text) <= 70:
        return text
    return None


def build_metadata(
    cfg: AppConfig,
    channel: str,
    display_name: str,
    moment: dt.datetime,
    summary: dict,
    transcript_text: str = "",
    shorts: bool = False,
) -> VideoMetadata:
    lang = cfg.text_language if cfg.text_language in TEXTS else "de"
    t = TEXTS[lang]
    s = display_name or channel
    date_str = moment.strftime("%d.%m.%Y") if lang == "de" else moment.strftime("%Y-%m-%d")
    time_str = moment.strftime("%H:%M")

    mood = _mood(summary)
    titles = [x.format(s=s, d=date_str) for x in t[mood]]
    random.Random(int(moment.timestamp())).shuffle(titles)
    # Zitat aus dem Transkript als zusätzlicher Vorschlag
    for sentence in _sentences(transcript_text):
        q = _good_quote(sentence)
        if q:
            titles.append(t["quote"].format(q=q, s=s))
            break
    # Häufigste echte Chat-Nachricht als Idee (keine reinen Emotes/Fragezeichen)
    for msg in summary.get("top_messages", [])[:5]:
        if _good_quote(msg["text"]) and msg.get("users", 0) >= 2:
            titles.append(t["quote"].format(q=msg["text"], s=s))
            break
    titles = [x[:100] for x in dict.fromkeys(titles)]  # YouTube: max. 100 Zeichen
    if shorts:
        titles = [x if len(x) <= 91 else x[:91] for x in titles]
        titles = [f"{x} #shorts" for x in titles]

    emotes = [e["text"] for e in summary.get("top_emotes", [])[:5]]
    chat_line = t["chat"].format(x=", ".join(f"{e['text']} ({e['users']})" for e in summary.get("top_emotes", [])[:5])) if emotes else ""
    hashtag_words = [channel, "twitch", "highlights"] + (["shorts"] if shorts else [])
    hashtags = " ".join("#" + w.replace(" ", "") for w in hashtag_words)
    description = t["desc"].format(
        title=titles[0], s=s, d=date_str, t=time_str, url=channel_url(channel), chat=chat_line, hashtags=hashtags,
    )

    tags = [s, channel, f"{channel} highlights", f"{s} twitch", "twitch", "twitch highlights", "stream highlights"]
    tags += [e for e in emotes if e.isascii() and len(e) > 2][:3]
    tags += list(cfg.youtube.extra_tags)
    if shorts:
        tags += ["shorts", "youtube shorts"]
    tags = _limit_tags(tags)
    return VideoMetadata(title=titles[0], title_alternatives=titles[1:], description=description, tags=tags, shorts=shorts)


def _sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text or "") if p.strip()]


def _limit_tags(tags: list[str], max_total: int = 450) -> list[str]:
    """YouTube erlaubt ca. 500 Zeichen Tags insgesamt – Duplikate entfernen, kürzen."""
    out, seen, total = [], set(), 0
    for tag in tags:
        tag = tag.strip().replace("<", "").replace(">", "").replace(",", " ")
        if not tag or tag.lower() in seen:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if total + cost > max_total:
            break
        out.append(tag)
        seen.add(tag.lower())
        total += cost
    return out


def write_metadata_files(meta: VideoMetadata, txt_path: Path, lang: str = "de") -> Path:
    """Schreibt eine gut lesbare .txt und eine .json (für den Upload)."""
    if lang == "en":
        head = ("TITLE", "ALTERNATIVE TITLES", "DESCRIPTION", "TAGS (comma separated)")
    else:
        head = ("TITEL", "ALTERNATIVE TITEL", "BESCHREIBUNG", "TAGS (kommagetrennt)")
    lines = [
        f"=== {head[0]} ===", meta.title, "",
        f"=== {head[1]} ===", *[f"- {t}" for t in meta.title_alternatives], "",
        f"=== {head[2]} ===", meta.description, "",
        f"=== {head[3]} ===", ", ".join(meta.tags), "",
    ]
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    txt_path.with_suffix(".json").write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8")
    return txt_path


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
class YouTubeUploadError(RuntimeError):
    pass


def upload_available(cfg: YouTubeConfig, app_cfg: AppConfig) -> tuple[bool, str]:
    """Prüft, ob ein Upload möglich ist, und liefert sonst den Grund."""
    if not cfg.upload_enabled:
        return False, "Upload ist in der config.yaml deaktiviert (youtube.upload_enabled)."
    try:
        import importlib

        importlib.import_module("googleapiclient")
        importlib.import_module("google_auth_oauthlib")
    except ImportError:
        return False, "Google-Bibliotheken fehlen (pip install google-api-python-client google-auth-oauthlib)."
    if not app_cfg.resolve_path(cfg.client_secrets_file).exists():
        return False, f"{cfg.client_secrets_file} nicht gefunden (siehe README → YouTube-Upload)."
    return True, ""


def _credentials(cfg: YouTubeConfig, app_cfg: AppConfig):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = app_cfg.resolve_path(cfg.token_file)
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, json.JSONDecodeError):
            creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # Token widerrufen o. Ä.
            log.warning("YouTube-Token konnte nicht erneuert werden (%s) – neue Anmeldung nötig.", exc)
            creds = None
    if not creds or not creds.valid:
        log.info("Öffne Browser für die YouTube-Anmeldung …")
        flow = InstalledAppFlow.from_client_secrets_file(str(app_cfg.resolve_path(cfg.client_secrets_file)), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def upload_video(video: Path, meta: VideoMetadata, app_cfg: AppConfig) -> str:
    """Lädt ein Video hoch (Standard: privat) und gibt die Video-ID zurück."""
    ok, reason = upload_available(app_cfg.youtube, app_cfg)
    if not ok:
        raise YouTubeUploadError(reason)
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    cfg = app_cfg.youtube
    service = build("youtube", "v3", credentials=_credentials(cfg, app_cfg), cache_discovery=False)
    body = {
        "snippet": {
            "title": meta.title[:100],
            "description": meta.description[:4900],
            "tags": meta.tags,
            "categoryId": cfg.category_id,
        },
        "status": {"privacyStatus": cfg.privacy_status, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    retries = 0
    log.info("Lade %s zu YouTube hoch (%s) …", video.name, cfg.privacy_status)
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info("Upload: %d %%", int(status.progress() * 100))
        except HttpError as exc:
            if exc.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                wait = 2 ** retries
                log.warning("YouTube-Serverfehler %s – neuer Versuch in %d s", exc.resp.status, wait)
                time.sleep(wait)
                continue
            raise YouTubeUploadError(f"YouTube-API-Fehler: {exc}") from exc
    video_id = response.get("id", "")
    log.info("Upload fertig: https://youtu.be/%s (Status: %s)", video_id, cfg.privacy_status)
    return video_id
