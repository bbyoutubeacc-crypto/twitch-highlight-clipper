# 🎬 Twitch Highlight Clipper

Beobachtet einen laufenden Twitch-Livestream, erkennt Highlights anhand der
Chat-Aktivität, schneidet sie automatisch als Clips (16:9 und/oder 9:16 für
Shorts), erzeugt Untertitel und bereitet alles für YouTube vor.

* **Weboberfläche** im Browser (lokal, Gradio) mit Live-Diagramm der Chat-Aktivität
* **Ringpuffer**: Der Stream wird ständig in 10-s-Stücken mitgeschnitten, die letzten ~2 Minuten liegen immer bereit
* **Erkennung nur über den Chat** (anonym, kein API-Key): Nachrichten pro Sekunde im Vergleich zum Normalwert + Bonus für Hype-Emotes
* **Untertitel** lokal mit faster-whisper (Deutsch/Englisch, automatische Spracherkennung), als `.srt` und eingebrannt
* **YouTube-Texte** (Titelvorschläge, Beschreibung mit Credit, Tags) und optionaler Upload als „privat“
* **Testmodus** (nur Chat, keine Aufnahme) und **Replay** zum schnellen Einstellen der Schwellenwerte

---

## Inhalt

1. [So funktioniert es](#so-funktioniert-es)
2. [Installation unter Windows](#installation-unter-windows)
3. [Installation unter macOS](#installation-unter-macos)
4. [Starten und Bedienung](#starten-und-bedienung)
5. [Schwellenwerte einstellen: Testmodus & Replay](#schwellenwerte-einstellen-testmodus--replay)
6. [Was wird gespeichert?](#was-wird-gespeichert)
7. [Einstellungen (config.yaml)](#einstellungen-configyaml)
8. [Untertitel](#untertitel)
9. [YouTube-Upload einrichten (optional)](#youtube-upload-einrichten-optional)
10. [Fehlerbehebung](#fehlerbehebung)
11. [Erweitern: Audio als zweites Signal](#erweitern-audio-als-zweites-signal)
12. [Projektstruktur & Tests](#projektstruktur--tests)

---

## So funktioniert es

```
 Twitch-Chat (IRC, anonym) ──► Chat-Signal ──► Detektor ──► Highlight!
                                (Rate vs. Normalwert,          │
                                 Hype-Bonus, Spam-Filter)      ▼
 Twitch-Stream ──► streamlink ──► ffmpeg ──► Ringpuffer ──► Clip schneiden
                                  (10-s-Segmente,           ├─ 16:9 / 9:16
                                   ~2 min, alte werden      ├─ Untertitel (.srt + eingebrannt)
                                   gelöscht)                ├─ Chat-Auswertung
                                                            └─ YouTube-Texte (+ Upload)
```

**Erkennung:** Pro Sekunde werden die (gefilterten) Chat-Nachrichten gezählt.
Die Rate der letzten 5 Sekunden wird mit dem Durchschnitt der letzten
3 Minuten verglichen. Ergebnis ist ein **Score = „x-fach über Normal“**. Bei
1 Nachricht/s normal und plötzlich 4/s ist der Score 4. Deshalb funktioniert
das bei kleinen und großen Kanälen gleich. Schreiben viele Leute Hype-Begriffe
(KEKW, POG, LETS GO, „clip“ …), steigt der Score zusätzlich. Ab der **Schwelle**
(Standard 3,0) gibt es ein Highlight, danach 60 s **Cooldown**.

**Stream-Delay:** Der Chat reagiert erst ein paar Sekunden *nach* dem
eigentlichen Moment, weil Zuschauer den Stream verzögert sehen und dann erst
tippen. Der erkannte Zeitpunkt wird deshalb um den eingestellten Delay
(Standard 6 s) nach vorne verschoben. Danach schneidet das Programm
**30 s davor bis 15 s danach**.

---

## Installation unter Windows

### 1. Python installieren (3.11 oder neuer)

1. <https://www.python.org/downloads/> öffnen und die aktuelle Version herunterladen.
2. Im Installer **unbedingt „Add python.exe to PATH“ anhaken**, dann „Install Now“.
3. Prüfen: Eingabeaufforderung (Win+R → `cmd`) öffnen und eingeben:
   ```bat
   python --version
   ```
   Es sollte `Python 3.11.x` oder höher erscheinen.

### 2. ffmpeg installieren

**Variante A – mit winget (am einfachsten, Windows 10/11):**
```bat
winget install --id Gyan.FFmpeg -e
```

**Variante B – manuell:**
1. <https://www.gyan.dev/ffmpeg/builds/> → `ffmpeg-release-essentials.zip` herunterladen.
2. Entpacken, z. B. nach `C:\ffmpeg`, sodass es `C:\ffmpeg\bin\ffmpeg.exe` gibt.
3. Windows-Suche → „Umgebungsvariablen“ → „Umgebungsvariablen bearbeiten“ →
   bei *Path* auf „Bearbeiten“ → „Neu“ → `C:\ffmpeg\bin` eintragen → OK.

**Danach ein neues** Eingabefenster öffnen und prüfen:
```bat
ffmpeg -version
```
> Wenn es partout nicht im PATH landen will: In der `config.yaml` bei
> `recording.ffmpeg_path` den vollen Pfad eintragen, z. B. `"C:/ffmpeg/bin/ffmpeg.exe"`.

### 3. Programm herunterladen

Den Projektordner herunterladen (z. B. „Code → Download ZIP“ auf GitHub) und
entpacken, etwa nach `C:\twitch-highlight-clipper`. Alternativ mit Git:
```bat
git clone <Repository-URL> C:\twitch-highlight-clipper
```

### 4. Virtuelle Umgebung + Pakete (inkl. streamlink)

```bat
cd C:\twitch-highlight-clipper
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```
Das installiert auch **streamlink** (als Python-Paket) – ein separater
streamlink-Installer ist nicht nötig. Prüfen:
```bat
python -m streamlink --version
```

> **PowerShell statt cmd?** Falls `.venv\Scripts\Activate.ps1` mit
> „Ausführung von Skripts ist deaktiviert“ abbricht, einmalig ausführen:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 5. Starten

```bat
cd C:\twitch-highlight-clipper
.venv\Scripts\activate
python app.py
```
Der Browser öffnet sich automatisch unter <http://127.0.0.1:7860>.

---

## Installation unter macOS

### 1. Homebrew (falls noch nicht vorhanden)

Terminal öffnen und den Befehl von <https://brew.sh> ausführen:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Am Ende zeigt der Installer zwei Befehle an („Next steps“). Die bitte
ebenfalls ausführen, damit `brew` gefunden wird.

### 2. Python und ffmpeg

```bash
brew install python@3.12 ffmpeg
python3 --version    # 3.11 oder höher
ffmpeg -version
```
> streamlink kommt im nächsten Schritt per `pip`. Wer es systemweit möchte,
> kann zusätzlich `brew install streamlink` ausführen. Das ist aber nicht nötig.

### 3. Programm + Pakete

```bash
git clone <Repository-URL> ~/twitch-highlight-clipper   # oder ZIP entpacken
cd ~/twitch-highlight-clipper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m streamlink --version
```

### 4. Starten

```bash
cd ~/twitch-highlight-clipper
source .venv/bin/activate
python app.py
```

---

## Starten und Bedienung

```bash
python app.py                    # Weboberfläche (öffnet den Browser)
python app.py --no-browser       # ohne automatisches Öffnen des Browsers
python app.py --port 7861        # anderer Port
python app.py --test   https://twitch.tv/kanal   # Testmodus in der Konsole
python app.py --headless https://twitch.tv/kanal # ohne UI aufnehmen + clippen
python app.py --replay logs/chat_kanal_….jsonl   # gespeicherten Chat auswerten
python app.py --debug            # ausführliches Logging
```

**In der Weboberfläche:**

1. Twitch-Link eingeben (z. B. `https://twitch.tv/kanalname` – auch nur `kanalname` geht).
2. Optional Einstellungen anpassen (gelten sofort, auch während des Laufs):
   * **Sekunden vor/nach dem Moment** – Clip-Länge (Standard 30 + 15 s)
   * **Stream-Delay** – wie viele Sekunden der Chat nach dem Moment reagiert
   * **Empfindlichkeit** – die Schwelle; *kleiner = mehr Highlights*
   * **Untertitel** an/aus
   * **Format**: 16:9, 9:16 (Shorts) oder beide
3. **Start** klicken. In der ersten Minute (Aufwärmphase) wird nur der
   Normalwert gelernt – erst danach können Highlights ausgelöst werden.
4. Oben siehst du den Status (Chat verbunden? Aufnahme läuft? Pufferfüllung),
   darunter die **Live-Diagramme**:
   * *Chat-Aktivität*: Nachrichten/s (grau), Durchschnitt der letzten Sekunden (lila), Normalwert (grün)
   * *Highlight-Score*: aktueller Score (lila) und Schwelle (rot). Kreuzt Lila die rote Linie → Highlight.
5. Fertige Clips erscheinen in der **Galerie** – anklicken zeigt die Vorschau,
   Details (Dateien, Titelvorschlag) und die Buttons **Ordner öffnen** und
   **Zu YouTube hochladen (privat)**.
6. **Stopp** beendet Chat und Aufnahme. Bereits erkannte Highlights werden
   noch fertig geschnitten und verarbeitet.

Ist der Stream offline, versucht das Programm es automatisch alle 30 s erneut.
Bricht die Chat- oder Stream-Verbindung ab, wird automatisch neu verbunden.

---

## Schwellenwerte einstellen: Testmodus & Replay

Jeder Kanal hat einen anderen Chat. So findest du schnell passende Werte:

**1. Testmodus** – beobachtet nur den Chat, nimmt nichts auf und loggt jedes
Highlight mit den häufigsten Emotes:
```bash
python app.py --test https://twitch.tv/kanalname
```
```
20:14:03 [INFO] 🔥 Highlight #1 erkannt – Score 3.21 (Schwelle 3.00)
20:14:18 [INFO] [TEST] Highlight #1 um 20:14:03 – Peak-Score 7.80 | Chat: KEKW (41), OMEGALUL (12), LUL (9)
```
(Geht auch in der Weboberfläche per Häkchen „Testmodus“ – dann siehst du
zusätzlich das Diagramm.)

**2. Replay** – Im Testmodus wird der Chat nebenbei in `logs/chat_<kanal>_<datum>.jsonl`
gespeichert. Diese Datei kannst du danach beliebig oft mit anderen
Einstellungen auswerten, in Sekunden statt Stunden:
```bash
python app.py --replay logs/chat_kanalname_2026-05-01_20-00-00.jsonl
python app.py --replay logs/chat_kanalname_2026-05-01_20-00-00.jsonl --threshold 2.5
python app.py --replay logs/chat_kanalname_2026-05-01_20-00-00.jsonl --threshold 4 --cooldown 90
```
```
  12873 Nachrichten über 92.4 Minuten
  Schwelle 2.50 | Fenster 5s | Normalwert 180s | Cooldown 60s | höchster Score 9.12
  => 7 Highlights
  #1   20:14:03  Score  2.61  Peak  7.80  | KEKW(41), OMEGALUL(12) | „KEKW“
  ...
```
Weitere Werte (Fenster, Hype-Gewichte, Mindestnachrichten …) änderst du in der
`config.yaml` und startest das Replay einfach erneut.

**Faustregeln**
* Zu viele Highlights → Schwelle hoch (3,5–5) oder `cooldown_seconds` erhöhen.
* Zu wenige → Schwelle runter (2–2,5) oder `hype_multiplier` erhöhen.
* Sehr kleiner Kanal (wenige Nachrichten/min) → `min_baseline_rate` und `min_messages` senken.
* Clip beginnt zu spät/früh → **Stream-Delay** anpassen (größer = Clip startet früher).

---

## Was wird gespeichert?

Pro Highlight ein Ordner `clips/<kanal>/<kanal>_<datum>_<uhrzeit>_score<x>/`:

| Datei | Inhalt |
|---|---|
| `…_score7.8.mp4` | Clip 16:9 |
| `…_score7.8_untertitelt.mp4` | 16:9 mit eingebrannten Untertiteln |
| `…_score7.8_shorts.mp4` | 9:16 (1080×1920, mittiger Ausschnitt) |
| `…_score7.8_shorts_untertitelt.mp4` | 9:16 mit großen, mittigen Untertiteln |
| `…_score7.8.srt` | Untertitel als Datei (z. B. für YouTube) |
| `…_score7.8_chat.txt` / `_chat.json` | Häufigste Nachrichten, Emotes, Wörter im Moment |
| `…_score7.8_youtube.txt` / `.json` | Titelvorschläge, Beschreibung, Tags |
| `…_score7.8_shorts_youtube.txt` | dasselbe für Shorts (mit #shorts) |
| `…_score7.8_thumb.jpg` | Vorschaubild |

Welche Videos entstehen, hängt von Format und Untertitel-Einstellung ab.
Uhrzeit = Zeitpunkt des Moments im Stream (lokale Zeit), Score = höchster
Score während des Highlights.

---

## Einstellungen (config.yaml)

Alle Einstellungen stehen kommentiert in der `config.yaml`. Die wichtigsten:

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `recording.quality` | `best` | Stream-Qualität (`720p60`, `480p` … spart CPU/Platz) |
| `recording.segment_seconds` | 10 | Länge der Puffer-Stücke |
| `recording.buffer_seconds` | 120 | so viel bleibt im Puffer (wird bei Bedarf automatisch erhöht) |
| `recording.stream_delay_seconds` | 6 | Verzögerung Moment → Chat-Reaktion |
| `recording.streamlink_args` | `[]` | z. B. `["--twitch-low-latency"]` |
| `detection.threshold` | 3.0 | Schwelle „x-fach über Normal“ |
| `detection.window_seconds` | 5 | kurzes Fenster (aktuelle Aktivität) |
| `detection.baseline_seconds` | 180 | langes Fenster (Normalwert) |
| `detection.warmup_seconds` | 60 | Aufwärmphase nach dem Start |
| `detection.cooldown_seconds` | 60 | Pause nach einem Highlight |
| `detection.hype_multiplier` | 0.6 | Einfluss der Hype-Begriffe |
| `detection.hype_terms` | KEKW, POG, … | **eigene Emotes/Begriffe mit Gewicht ergänzen** |
| `detection.ignored_users` | Nightbot, … | Bots, die ignoriert werden |
| `detection.spam_window_seconds` | 30 | gleiche Nachricht desselben Users zählt nur 1× |
| `detection.max_messages_per_user` | 3 | max. gezählte Nachrichten pro User im Fenster |
| `clips.pre_seconds` / `post_seconds` | 30 / 15 | Clip-Länge |
| `clips.format` | `16:9` | `16:9`, `9:16` oder `both` |
| `subtitles.model` | `small` | Whisper-Modell |
| `text_language` | `de` | Sprache der YouTube-Texte (`de`/`en`) |

**Eigene Emotes ergänzen** – einfach in die Liste schreiben:
```yaml
  hype_terms:
    KEKW: 1.0
    meinKanalHype: 1.5     # Kanal-Emote zählt stärker
    "GG": 0.8
```
Einzelne Wörter werden als ganzes Wort gesucht (Groß-/Kleinschreibung egal),
Begriffe mit Leerzeichen („LETS GO“) als Textstück.

---

## Untertitel

* Transkription läuft **lokal** mit [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
  es werden keine Daten verschickt.
* Beim **ersten Clip** wird das Modell heruntergeladen (`small` ≈ 500 MB) – das
  dauert einmalig etwas.
* Modelle (`subtitles.model`): `tiny`, `base` (schnell, ungenauer), **`small`**
  (guter Kompromiss), `medium`, `large-v3` (beste Qualität, langsam ohne GPU).
* Sprache wird automatisch erkannt. Wer immer Deutsch streamt, kann
  `subtitles.language: "de"` setzen. Das ist robuster bei kurzen Clips.
* **NVIDIA-GPU**: `device: "auto"` nutzt CUDA automatisch, wenn CUDA 12 und
  cuDNN 9 installiert sind. Ohne GPU läuft alles auf der CPU (`int8`).
* Stil: weiße Schrift mit schwarzer Kontur, unten zentriert. Bei Shorts größer
  und mittig. Schriftgröße/-art und Zeilenlänge sind in der `config.yaml` einstellbar.
* Typische Whisper-„Halluzinationen“ bei Stille/Musik („Untertitel im Auftrag
  des ZDF“, „Thanks for watching“) werden automatisch herausgefiltert.

---

## YouTube-Upload einrichten (optional)

Ohne diese Einrichtung funktioniert alles andere ganz normal – du lädst die
Clips dann einfach selbst hoch und kopierst Titel/Beschreibung aus der
`_youtube.txt`.

1. <https://console.cloud.google.com/> öffnen → neues Projekt anlegen.
2. **APIs & Dienste → Bibliothek** → „YouTube Data API v3“ suchen → **Aktivieren**.
3. **APIs & Dienste → OAuth-Zustimmungsbildschirm**: Typ „Extern“, App-Name
   eintragen, eigene E-Mail angeben. Unter **Testnutzer** dein
   Google-/YouTube-Konto hinzufügen.
4. **APIs & Dienste → Anmeldedaten → Anmeldedaten erstellen → OAuth-Client-ID**,
   Anwendungstyp **„Desktop-App“**. Danach **JSON herunterladen**.
5. Die Datei als `client_secret.json` in den Programmordner legen (neben `app.py`).
6. In der `config.yaml`:
   ```yaml
   youtube:
     upload_enabled: true
     auto_upload: false     # true = jeder Clip wird automatisch hochgeladen
   ```
7. Beim ersten Upload öffnet sich der Browser zur Anmeldung. Der Zugang wird in
   `youtube_token.json` gespeichert.

Hinweise:
* Videos werden immer als **privat** hochgeladen (`privacy_status`), du prüfst sie
  vorher und schaltest sie selbst frei. Bei nicht verifizierten Google-Projekten
  erzwingt YouTube ohnehin „privat“.
* Die API hat ein Tageskontingent (Standard 10.000 Einheiten, ein Upload ≈ 1.600)
  – also etwa **6 Uploads pro Tag**.
* `client_secret.json` und `youtube_token.json` sind Zugangsdaten – **niemals
  teilen oder committen** (stehen bereits in `.gitignore`).

---

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| `ffmpeg wurde nicht gefunden` | ffmpeg installieren (siehe oben), **neues** Terminal öffnen, oder `recording.ffmpeg_path` setzen |
| `Stream offline – neuer Versuch in 30 s` | Kanal ist nicht live – das Programm wartet automatisch |
| Status bleibt bei „prüfe Stream …“ | Log aufklappen; ggf. `recording.quality: "720p"` testen; `python -m streamlink https://twitch.tv/kanal` manuell probieren |
| Chat verbindet nicht | Firewall/Proxy? Port 6697 (SSL) muss erreichbar sein. Notfalls `chat.port: 6667` und `chat.use_ssl: false` |
| Keine Highlights | Aufwärmphase (1 min) abwarten; Diagramm prüfen – erreicht der Score die rote Linie? Schwelle senken |
| Zu viele Highlights | Schwelle erhöhen, Cooldown verlängern |
| Clip ist „gekürzt“ | Der Moment lag vor dem Aufnahmestart bzw. der Stream hatte eine Lücke (Werbung, Abbruch) |
| Clip-Inhalt passt zeitlich nicht | **Stream-Delay** anpassen (Clip zu spät → Delay erhöhen) |
| Untertitel sehr langsam | kleineres Modell (`base`) oder GPU nutzen; Untertitel ggf. abschalten |
| Hohe CPU-Last | `recording.quality: "720p60"` und `clips.video_preset: "veryfast"` |
| Port 7860 belegt | `python app.py --port 7861` |

Ausführliches Log: `logs/clipper.log` (mit `--debug` noch detaillierter).

---

## Erweitern: Audio als zweites Signal

Die Erkennung ist modular (`clipper/detection/`). Jedes Signal liefert einen
Score „x-fach über Normal“, der Detektor bildet daraus den gewichteten
Mittelwert. Ein Audio-Signal (z. B. Lautstärke/Schreie) sähe so aus:

```python
# clipper/detection/audio_signal.py
from .base import Signal, SignalSample

class AudioLoudnessSignal(Signal):
    name = "audio"

    def feed(self, timestamp: float, rms: float) -> None:
        ...  # z. B. von einem ffmpeg-Prozess mit "astats" gefüttert

    def update(self, now: float) -> SignalSample:
        ratio = aktuelle_lautstaerke / normale_lautstaerke
        return SignalSample(time=now, score=ratio, value=aktuelle_lautstaerke)

    def is_ready(self, now: float) -> bool:
        return genug_daten_gesammelt
```

Dann in `clipper/session.py` zusätzlich übergeben:
```python
self.detector = HighlightDetector(signals=[self.chat_signal, self.audio_signal], ...)
```
und in der `config.yaml` gewichten:
```yaml
  signal_weights:
    chat: 1.0
    audio: 0.7
```

---

## Projektstruktur & Tests

```
app.py                     Startpunkt (UI, --test, --headless, --replay)
config.yaml                Einstellungen
clipper/
  config.py                Laden + Prüfen der Konfiguration
  chat.py                  Anonymer Twitch-IRC-Client mit Auto-Reconnect
  detection/
    base.py                Signal-Interface (für Chat, später Audio …)
    chat_signal.py         Chat-Aktivität, Hype-Begriffe, Spam-/Bot-Filter
    detector.py            Kombination, Schwelle, Cooldown, Verlauf
  recorder.py              streamlink → ffmpeg → Ringpuffer (+ Uhrzeit-Zuordnung)
  clip_maker.py            Schneiden, 9:16, Untertitel einbrennen, Vorschaubild
  subtitles.py             faster-whisper, .srt/.ass
  youtube.py               Titel/Beschreibung/Tags, Upload
  session.py               Orchestrierung (Threads, Warteschlangen)
  replay.py                Gespeicherten Chat erneut auswerten
  ui.py                    Gradio-Oberfläche
tests/                     pytest-Tests
```

Tests ausführen:
```bash
pip install -r requirements-dev.txt
python -m pytest
```

---

## Rechtliches

Clips aus fremden Streams sind urheberrechtlich geschützt. Lade Clips nur hoch,
wenn der Streamer einverstanden ist (viele erlauben Clip-Kanäle ausdrücklich,
manche nicht). Nenne immer den Streamer und verlinke den Kanal. Das erledigt
die generierte Beschreibung automatisch.
