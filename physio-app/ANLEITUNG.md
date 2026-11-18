# Knie-Kraft – deine Physio-App bis zur OP

## Was ist in diesem Ordner?

| Datei | Wozu |
|---|---|
| `index.html` | Die ganze App (Aussehen, Übungen, Logik) |
| `sw.js` | Macht die App offline nutzbar |
| `manifest.webmanifest` | Name und Symbol für den Home-Bildschirm |
| `icon.svg`, `icon-192.png`, `icon-512.png`, `apple-touch-icon.png` | App-Symbol |

Lade **immer den ganzen Ordner** hoch, nicht nur `index.html`.

---

## Weg A: Netlify Drop (am einfachsten, ca. 5 Minuten)

1. Lade diesen Ordner `physio-app` auf deinen Computer herunter.
2. Öffne am Computer **https://app.netlify.com/drop**.
3. Erstelle ein kostenloses Konto (z. B. mit E-Mail). Ohne Konto wird die Seite nach einer Stunde gelöscht.
4. Ziehe den ganzen Ordner `physio-app` mit der Maus in das Feld auf der Seite.
5. Nach ein paar Sekunden siehst du eine Adresse wie `https://irgendwas-123.netlify.app`. Das ist deine App.
6. Optional: Unter **Site configuration → Change site name** kannst du einen schöneren Namen wählen, z. B. `knie-kraft-max.netlify.app`.
7. Schick dir die Adresse aufs Handy (per Mail oder Nachricht).

## Weg B: GitHub Pages

1. Auf **github.com** einloggen und oben rechts **+ → New repository** wählen.
2. Name z. B. `knie-kraft`, **Public** auswählen und **Create repository** klicken.
3. Auf **uploading an existing file** klicken und **alle Dateien aus dem Ordner** `physio-app` hineinziehen (die Dateien selbst, nicht den Ordner). Dann **Commit changes**.
4. Im Repository: **Settings → Pages**. Bei **Branch** `main` und `/ (root)` wählen und **Save**.
5. Nach 1–2 Minuten erscheint oben die Adresse, z. B. `https://deinname.github.io/knie-kraft/`.

---

## Auf dem Handy installieren

**Wichtig:** Installiere die App gleich am Anfang und benutze danach nur noch das Symbol auf dem Home-Bildschirm. Auf dem iPhone speichern Safari und die installierte App den Fortschritt getrennt.

### iPhone (Safari)
1. Öffne die Adresse in **Safari** (nicht in Chrome oder einer anderen App).
2. Tippe unten auf **Teilen** (Quadrat mit Pfeil nach oben).
3. Scrolle nach unten und wähle **«Zum Home-Bildschirm»**.
4. Tippe auf **«Hinzufügen»**. Jetzt ist das grüne Knie-Kraft-Symbol auf deinem Home-Bildschirm.

### Android (Chrome)
1. Öffne die Adresse in **Chrome**.
2. Tippe auf **⋮** (drei Punkte oben rechts).
3. Wähle **«App installieren»** oder **«Zum Startbildschirm hinzufügen»** und bestätige.

---

## Erinnerungen

Eine Web-App ohne eigenen Server kann nicht zuverlässig zu festen Zeiten Mitteilungen schicken. Auf dem iPhone geht das gar nicht, auf Android nur unzuverlässig. So kommst du trotzdem zu Erinnerungen:

- **Empfohlen:** In der App unter **Einstellungen → Erinnerungen** deine Zeiten wählen und **«Erinnerungen in den Kalender übernehmen»** tippen. Damit landen 32 Termine mit Alarm (und der OP-Tag) in deinem Kalender.
  iPhone: Das klappt am besten, wenn du die Seite dafür einmal in **Safari** öffnest (nicht über das Home-Symbol) und dann **«Alle hinzufügen»** tippst.
- **Alternative:** In der Uhr-App zwei tägliche Wecker stellen (z. B. 08:00 und 19:00) oder in der App **Erinnerungen** zwei Erinnerungen mit **«Wiederholen: Täglich»** anlegen.

## Gut zu wissen

- Dein Fortschritt wird nur auf deinem Handy gespeichert. Es gibt kein Konto, und nichts wird ins Internet geschickt.
- Unter **Einstellungen → Daten** kannst du eine Sicherung herunterladen.
- **iPhone:** Vibration unterstützt Safari nicht. Für den Ton am Ende der 10 Sekunden muss der Stumm-Schalter aus sein. Zusätzlich blinkt der Bildschirm kurz auf.
- **Vorschau des OP-Tags:** Hänge `?heute=2026-12-04` an die Adresse, um den Abschlussbildschirm schon jetzt anzuschauen. Solange du dort nichts abhakst, ändert das nichts an deinen Daten.
