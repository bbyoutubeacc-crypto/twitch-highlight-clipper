"""Weboberfläche (Gradio) – läuft lokal im Browser."""

from __future__ import annotations

import datetime as dt
import inspect
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import gradio as gr
import pandas as pd

from .config import AppConfig
from .logging_setup import memory_handler
from .session import ClipOptions, JobState, Session, check_tools
from .utils import format_duration, parse_channel
from .youtube import upload_available

log = logging.getLogger(__name__)

FORMAT_CHOICES = {"16:9": "16:9", "9:16 (Shorts)": "9:16", "Beide": "both"}
FORMAT_LABELS = {v: k for k, v in FORMAT_CHOICES.items()}

CSS = """
#status-box {font-size: 0.95rem}
#log-box textarea {font-family: ui-monospace, Consolas, monospace; font-size: 12px}
"""


class Controller:
    """Hält die aktuelle Sitzung (eine pro Programm – die App läuft lokal)."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.session: Session | None = None
        self.transcriber = None  # Whisper-Modell über Sitzungen hinweg behalten
        self.lock = threading.Lock()

    def start(self, link: str, test_mode: bool, options: ClipOptions) -> str:
        with self.lock:
            channel = parse_channel(link)  # wirft ValueError mit verständlichem Text
            problems = check_tools(self.cfg, test_mode)
            if problems:
                raise RuntimeError(" ".join(problems))
            if self.session and self.session.running:
                self._stop_locked()
            self.session = Session(self.cfg, link, test_mode=test_mode, options=options, transcriber=self.transcriber)
            self.session.start()
            return channel

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self.session:
            if self.session.transcriber is not None:
                self.transcriber = self.session.transcriber
            self.session.stop(wait_for_processing=False)


# ---------------------------------------------------------------------------
# Darstellungs-Helfer
# ---------------------------------------------------------------------------
def _status_markdown(session: Session | None) -> str:
    if session is None:
        return "⏸️ **Bereit.** Twitch-Link eingeben und auf **Start** klicken."
    s = session.status()
    if not s["running"]:
        return f"⏹️ **Gestoppt** – #{s['channel']} · {s['highlights']} Highlights erkannt."
    parts = [
        f"🟢 **#{s['channel']}**" + (" · 🧪 **TESTMODUS** (keine Aufnahme)" if s["test_mode"] else ""),
        f"⏱️ {format_duration(s['uptime'])}",
        f"💬 Chat: {s['chat_status']} ({s['chat_messages']} Nachr., {s['ignored_spam']} Spam, {s['ignored_bot']} Bot)",
    ]
    if not s["test_mode"]:
        parts.append(f"🎥 Aufnahme: {s['recorder_status']} · Puffer {s['buffered_seconds']:.0f} s")
    parts.append(f"🔥 Highlights: {s['highlights']}")
    line2 = ""
    if s["warming_up"]:
        line2 = f"\n\n⏳ Aufwärmphase – Normalwert wird ermittelt, Erkennung startet in {s['warmup_left']:.0f} s."
    elif s["cooldown"]:
        line2 = "\n\n🧊 Cooldown nach dem letzten Highlight."
    return " · ".join(parts) + line2


RATE_SERIES = ("Nachrichten/s", "Ø letzte Sekunden", "Normalwert")
SCORE_SERIES = ("Score", "Schwelle")


def _frame(rows: list[tuple[float, float, str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["Sekunden", "Wert", "Reihe"])
    return df.astype({"Sekunden": "float64", "Wert": "float64", "Reihe": "object"})


def _placeholder(series: tuple[str, ...], seconds: float, values: dict[str, float] | None = None) -> pd.DataFrame:
    """Gradio-Plots, die leer starten, zeichnen später keine Linien – daher
    immer mit (flachen) Platzhalter-Linien für alle Reihen beginnen."""
    values = values or {}
    return _frame([(x, values.get(name, 0.0), name) for name in series for x in (-float(seconds), 0.0)])


def _idle_frames(seconds: float, threshold: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        _placeholder(RATE_SERIES, seconds, {"Normalwert": 1.0}),
        _placeholder(SCORE_SERIES, seconds, {"Schwelle": threshold}),
    )


def _chart_frames(session: Session | None, seconds: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    if session is None:
        return _idle_frames(seconds, 3.0)
    hist = session.detector.history(seconds)
    if not hist:
        return _idle_frames(seconds, session.detector.threshold)
    now = hist[-1].time
    rate_rows, score_rows = [], []
    for h in hist:
        x = round(h.time - now, 1)
        chat = h.signals.get("chat")
        if chat is not None:
            rate_rows.append((x, chat.value, "Nachrichten/s"))
            rate_rows.append((x, round(chat.details.get("rate", 0.0), 2), "Ø letzte Sekunden"))
            rate_rows.append((x, round(chat.baseline, 2), "Normalwert"))
        score_rows.append((x, round(h.score, 2), "Score"))
        score_rows.append((x, h.threshold, "Schwelle"))
    return _frame(rate_rows), _frame(score_rows)


def _jobs_table(session: Session | None) -> pd.DataFrame:
    cols = ["#", "Zeit", "Score", "Status", "Chat-Reaktion", "Ordner"]
    if session is None:
        return pd.DataFrame(columns=cols)
    rows = []
    for j in reversed(session.jobs_snapshot()):
        status = j.state.value + (f" – {j.error}" if j.error else "")
        rows.append([
            j.event.id,
            dt.datetime.fromtimestamp(j.event.trigger_time).strftime("%H:%M:%S"),
            round(j.score, 2),
            status,
            j.top_chat,
            j.folder.name if j.folder else "",
        ])
    return pd.DataFrame(rows, columns=cols)


def _gallery_items(session: Session | None) -> list[tuple[str, str]]:
    if session is None:
        return []
    items = []
    for j in reversed(session.jobs_snapshot()):
        thumb = j.files.get("thumbnail")
        if thumb and thumb.exists():
            caption = f"#{j.event.id} · {dt.datetime.fromtimestamp(j.event.trigger_time):%H:%M:%S} · Score {j.score:.1f}"
            items.append((str(thumb), caption))
    return items


def _job_details(job) -> str:
    if job is None:
        return ""
    lines = [f"### Highlight #{job.event.id} – Score {job.score:.2f}", f"**Status:** {job.state.value}"]
    if job.error:
        lines.append(f"**Hinweis:** {job.error}")
    if job.language:
        lines.append(f"**Sprache:** {job.language}")
    if job.top_chat:
        lines.append(f"**Chat:** {job.top_chat}")
    if job.folder:
        lines.append(f"**Ordner:** `{job.folder}`")
    files = [p.name for k, p in job.files.items() if p.exists()]
    if files:
        lines.append("**Dateien:** " + ", ".join(f"`{f}`" for f in files))
    txt = job.files.get("youtube_txt") or job.files.get("shorts_youtube_txt")
    if txt and txt.exists():
        title = txt.read_text(encoding="utf-8").splitlines()[1]
        lines.append(f"**Titelvorschlag:** {title}")
    if job.youtube_ids:
        lines.append("**YouTube:** " + ", ".join(f"https://youtu.be/{v}" for v in job.youtube_ids.values()))
    return "\n\n".join(lines)


def _open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def build_ui(cfg: AppConfig) -> tuple[gr.Blocks, Controller]:
    ctl = Controller(cfg)
    chart_seconds = cfg.ui.chart_seconds
    theme = gr.themes.Soft(primary_hue="violet")
    blocks_kwargs = {"title": "Twitch Highlight Clipper"}
    if "theme" in inspect.signature(gr.Blocks.__init__).parameters:  # Gradio 4/5
        blocks_kwargs.update(theme=theme, css=CSS)

    with gr.Blocks(**blocks_kwargs) as demo:
        gr.Markdown("# 🎬 Twitch Highlight Clipper\nBeobachtet den Chat, erkennt Highlights und schneidet sie automatisch.")

        with gr.Row(equal_height=True):
            link = gr.Textbox(label="Twitch-Link", placeholder="https://twitch.tv/kanalname", scale=5)
            start_btn = gr.Button("▶ Start", variant="primary", scale=1)
            stop_btn = gr.Button("⏹ Stopp", variant="stop", scale=1)
        test_mode = gr.Checkbox(
            label="🧪 Testmodus – nur Chat beobachten und Highlights loggen (keine Aufnahme)", value=False,
        )

        with gr.Accordion("⚙️ Einstellungen", open=True):
            with gr.Row():
                pre = gr.Slider(5, 120, value=cfg.clips.pre_seconds, step=1, label="Sekunden VOR dem Moment")
                post = gr.Slider(3, 60, value=cfg.clips.post_seconds, step=1, label="Sekunden NACH dem Moment")
                delay = gr.Slider(
                    0, 30, value=cfg.recording.stream_delay_seconds, step=0.5,
                    label="Stream-Delay (s): so viel später reagiert der Chat",
                )
            with gr.Row():
                threshold = gr.Slider(
                    1.2, 10, value=cfg.detection.threshold, step=0.1,
                    label="Empfindlichkeit: Schwelle (x-fach über Normal) – kleiner = mehr Highlights",
                )
                subs = gr.Checkbox(label="Untertitel (faster-whisper)", value=cfg.subtitles.enabled)
                fmt = gr.Radio(
                    list(FORMAT_CHOICES), value=FORMAT_LABELS.get(cfg.clips.format, "16:9"), label="Format",
                )
            gr.Markdown("<small>Änderungen gelten sofort – auch während eine Sitzung läuft.</small>")

        status = gr.Markdown(_status_markdown(None), elem_id="status-box")

        with gr.Row():
            rate_plot = gr.LinePlot(
                value=_idle_frames(chart_seconds, cfg.detection.threshold)[0], x_lim=[-chart_seconds, 0],
                x="Sekunden", y="Wert", color="Reihe", title="Chat-Aktivität",
                x_title="Sekunden (0 = jetzt)", y_title="Nachrichten pro Sekunde", height=260, min_width=320,
                color_map={"Nachrichten/s": "#9aa4b2", "Ø letzte Sekunden": "#7c3aed", "Normalwert": "#10b981"},
            )
            score_plot = gr.LinePlot(
                value=_idle_frames(chart_seconds, cfg.detection.threshold)[1], x_lim=[-chart_seconds, 0],
                x="Sekunden", y="Wert", color="Reihe", title="Highlight-Score",
                x_title="Sekunden (0 = jetzt)", y_title="x-fach über Normal", height=260, min_width=320,
                color_map={"Score": "#7c3aed", "Schwelle": "#ef4444"},
            )

        gr.Markdown("## 🔥 Highlights")
        with gr.Row():
            with gr.Column(scale=3, min_width=320):
                gallery = gr.Gallery(
                    label="Clips (anklicken für Vorschau)", columns=2, height=360, allow_preview=False, object_fit="contain",
                )
            with gr.Column(scale=4, min_width=320):
                video = gr.Video(label="Vorschau", height=360, interactive=False)
        details = gr.Markdown("")
        with gr.Row():
            upload_btn = gr.Button("⬆️ Ausgewählten Clip zu YouTube hochladen (privat)")
            folder_btn = gr.Button("📂 Ordner öffnen")
        action_msg = gr.Markdown("")
        table = gr.Dataframe(value=_jobs_table(None), label="Alle erkannten Highlights", interactive=False, wrap=True)

        with gr.Accordion("📜 Log", open=False):
            log_box = gr.Textbox(lines=14, max_lines=14, show_label=False, elem_id="log-box", autoscroll=True)

        ui_state = gr.State({"sig": None, "selected": None, "shown": None})
        timer = gr.Timer(1.0)

        # ------------------------------------------------------------ Handler
        def options_from(pre_v, post_v, delay_v, thr_v, subs_v, fmt_v) -> ClipOptions:
            return ClipOptions(
                pre_seconds=pre_v, post_seconds=post_v, threshold=thr_v, stream_delay=delay_v,
                subtitles=subs_v, format=FORMAT_CHOICES.get(fmt_v, "16:9"),
            )

        def on_start(link_v, test_v, pre_v, post_v, delay_v, thr_v, subs_v, fmt_v):
            try:
                channel = ctl.start(link_v, test_v, options_from(pre_v, post_v, delay_v, thr_v, subs_v, fmt_v))
            except Exception as exc:
                log.error("Start fehlgeschlagen: %s", exc)
                gr.Warning(str(exc))
                return _status_markdown(ctl.session), {"sig": None, "selected": None, "shown": None}
            gr.Info(f"Beobachte #{channel}" + (" im Testmodus" if test_v else ""))
            return _status_markdown(ctl.session), {"sig": None, "selected": None, "shown": None}

        def on_stop():
            ctl.stop()
            return _status_markdown(ctl.session)

        def on_settings(pre_v, post_v, delay_v, thr_v, subs_v, fmt_v):
            s = ctl.session
            if s is None or not s.running:
                return
            o = options_from(pre_v, post_v, delay_v, thr_v, subs_v, fmt_v)
            s.update_options(o)

        def find_job(job_id):
            s = ctl.session
            if s is None or job_id is None:
                return None
            return next((j for j in s.jobs_snapshot() if j.event.id == job_id), None)

        def on_select(state, evt: gr.SelectData):
            s = ctl.session
            if s is None:
                return gr.update(), "", state
            done = [j for j in reversed(s.jobs_snapshot()) if j.files.get("thumbnail")]
            if evt.index is None or evt.index >= len(done):
                return gr.update(), "", state
            job = done[evt.index]
            state = dict(state, selected=job.event.id, shown=(job.event.id, str(job.preview)))
            return (str(job.preview) if job.preview else None), _job_details(job), state

        def on_refresh(state):
            s = ctl.session
            rate_df, score_df = _chart_frames(s, chart_seconds)
            jobs = s.jobs_snapshot() if s else []
            sig = tuple((j.event.id, j.state, bool(j.files.get("thumbnail"))) for j in jobs)
            gallery_upd = gr.update()
            video_upd = gr.update()
            details_upd = gr.update()
            if sig != state.get("sig"):
                gallery_upd = _gallery_items(s)
                state = dict(state, sig=sig)
                # Automatisch den neuesten fertigen Clip zeigen, solange der
                # Nutzer nichts ausgewählt hat
                if state.get("selected") is None:
                    latest = next((j for j in reversed(jobs) if j.state == JobState.DONE and j.preview), None)
                    if latest and (state.get("shown") or (None,))[0] != latest.event.id:
                        video_upd = str(latest.preview)
                        state = dict(state, shown=(latest.event.id, str(latest.preview)))
                shown_id = (state.get("shown") or (None,))[0]
                details_upd = _job_details(find_job(state.get("selected") or shown_id))
            return (
                _status_markdown(s), rate_df, score_df, gallery_upd, video_upd, details_upd,
                _jobs_table(s), memory_handler.text(120), state,
            )

        def current_job(state):
            shown = state.get("shown") or (None,)
            return find_job(state.get("selected") or shown[0])

        def on_upload(state):
            job = current_job(state)
            if job is None or ctl.session is None:
                return "⚠️ Bitte zuerst einen fertigen Clip auswählen."
            ok, reason = upload_available(ctl.cfg.youtube, ctl.cfg)
            if not ok:
                return f"⚠️ {reason}"
            if job.state != JobState.DONE:
                return "⚠️ Der Clip ist noch nicht fertig verarbeitet."
            try:
                urls = ctl.session.upload(job)
            except Exception as exc:
                log.exception("Upload fehlgeschlagen")
                return f"❌ Upload fehlgeschlagen: {exc}"
            return "✅ Hochgeladen (privat): " + ", ".join(urls) if urls else "ℹ️ Bereits hochgeladen."

        def on_folder(state):
            job = current_job(state)
            path = job.folder if job and job.folder else ctl.cfg.output_path
            path.mkdir(parents=True, exist_ok=True)
            try:
                _open_folder(path)
            except Exception as exc:
                return f"Ordner: `{path}` ({exc})"
            return f"📂 `{path}`"

        settings = [pre, post, delay, threshold, subs, fmt]
        start_btn.click(on_start, [link, test_mode, *settings], [status, ui_state])
        link.submit(on_start, [link, test_mode, *settings], [status, ui_state])
        stop_btn.click(on_stop, None, status)
        for comp in settings:
            comp.change(on_settings, settings, None)
        gallery.select(on_select, ui_state, [video, details, ui_state])
        timer.tick(
            on_refresh, ui_state,
            [status, rate_plot, score_plot, gallery, video, details, table, log_box, ui_state],
            show_progress="hidden",
        )
        upload_btn.click(on_upload, ui_state, action_msg)
        folder_btn.click(on_folder, ui_state, action_msg)

    return demo, ctl


def launch(cfg: AppConfig, open_browser: bool = True) -> None:
    demo, ctl = build_ui(cfg)
    cfg.output_path.mkdir(parents=True, exist_ok=True)
    launch_kwargs = dict(
        server_name=cfg.ui.host,
        server_port=cfg.ui.port,
        inbrowser=open_browser,
        allowed_paths=[str(cfg.output_path)],
        show_error=True,
    )
    params = inspect.signature(gr.Blocks.launch).parameters
    if "theme" in params:  # Gradio 6+
        launch_kwargs.update(theme=gr.themes.Soft(primary_hue="violet"), css=CSS)
    log.info("Weboberfläche: http://%s:%d", cfg.ui.host, cfg.ui.port)
    try:
        demo.queue(default_concurrency_limit=4).launch(**launch_kwargs)
    finally:
        ctl.stop()
        if ctl.session is not None:
            ctl.session.wait_for_processing()
