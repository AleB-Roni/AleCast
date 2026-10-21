# AleCast

Streamt den Bildschirm eines per USB angeschlossenen Android-Handys als RTSP-Stream – gedacht als Notlösung, um die Tapo-App-Live-Ansicht in Frigate aufzunehmen.

```
Handy (Tapo-App) --USB--> scrcpy-Server (H.264) --> ffmpeg --> RTSP-Push --> go2rtc/Frigate
```

- Nutzt den offiziellen scrcpy-Server (v4.0) im Rohstrom-Modus: kein Zeitlimit wie bei `screenrecord`.
- Startet bei Abbruch oder Ab-/Anstecken automatisch neu.
- Tippt den „Weiterschauen“-Dialog der Tapo-App automatisch weg (Auto-Tap).
- Tippt alle 10 Minuten in die Bildschirmmitte, falls der Feed hängt und „Aktualisieren“ braucht.
- **Web-UI** zum Fernsteuern des Handys: Bildschirm sehen, tippen, wischen, Tasten, Text, Tapo/Stream neu starten.
- Hält das Display wach, solange das Handy per USB verbunden ist.

## 1. Handy vorbereiten

1. **Entwickleroptionen** freischalten (Einstellungen → Telefoninfo → Softwareinformationen → 7× auf „Buildnummer“ tippen).
2. In den Entwickleroptionen **USB-Debugging** und **Aktiviert lassen** (Display bleibt beim Laden an) einschalten.
3. Displaysperre auf **Keine** oder **Wischen** stellen.
4. Bei Dauerbetrieb am Ladekabel den Akkuschutz aktivieren (Samsung: Akku → Akkuschutz, z. B. max. 80 %).
5. Tapo-App öffnen, mit dem **Besitzerkonto** anmelden, Kamera im **Vollbild/Querformat** öffnen.

## 2. Frigate (go2rtc) vorbereiten

```yaml
go2rtc:
  streams:
    tapo_app:        # leer = wartet auf den eingehenden Push von AleCast

cameras:
  tapo_app:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/tapo_app
          input_args: preset-rtsp-restream
          roles: [detect, record]
    detect:
      width: 1280
      height: 720
      fps: 5
    record:
      enabled: true
      retain:
        days: 7
```

## 3. Auf Unraid installieren

Das Image wird bei jedem Push automatisch per GitHub Actions gebaut und liegt unter `ghcr.io/aleb-roni/alecast:latest`.

### Variante A: Über die Unraid-Oberfläche (Vorlage)

1. **Docker** → ganz unten **Template Repositories** → diese Zeile eintragen und **Speichern**:
   ```
   https://github.com/AleB-Roni/AleCast
   ```
2. **Add Container** → bei **Template** unter „User templates“ bzw. dem Repo **AleCast** wählen.
3. **RTSP-Ziel** auf die IP deines Frigate anpassen → **Apply**.

### Variante B: Terminal

```bash
docker run -d --name alecast \
  --restart unless-stopped \
  --privileged \
  -v /dev/bus/usb:/dev/bus/usb \
  -v /mnt/user/appdata/alecast/adbkeys:/root/.android \
  -e RTSP_URL=rtsp://192.168.1.10:8554/tapo_app \
  ghcr.io/aleb-roni/alecast:latest
```

`RTSP_URL` auf die IP deines Frigate-Servers anpassen.

Beim ersten Start fragt das Handy nach der **USB-Debugging-Freigabe** → „Immer von diesem Computer zulassen“ antippen. Der Schlüssel liegt danach in `adbkeys`, die Frage kommt nicht wieder.

Log ansehen:

```bash
docker logs -f alecast
```

## Web-UI

Port `8080` im Container auf einen freien Host-Port legen (Vorlage: `8086`), dann im Browser `http://<Unraid-IP>:8086` öffnen.

- **Klicken** = tippen, **ziehen** = wischen, **gedrückt halten** = langes Tippen
- Knöpfe für Zurück/Home/Apps, Entsperren, Mitte tippen, Tapo öffnen/neu starten, Stream neu starten, Quer-/Hochformat
- Text ans Handy senden (nur ASCII, keine Umlaute – Einschränkung von `adb input text`)
- Status: Akku, Temperatur, ob Tapo im Vordergrund ist, AleCast-Log

### Aufnahmebereich (Ausschnitt & Skalierung)

Karte **Aufnahmebereich**: **Bereich ziehen** aktivieren und ein Rechteck über das Bild ziehen, eine Vorlage wählen (Links/Rechts/Oben/Unten ½, Zentrum) oder X/Y/Breite/Höhe in Prozent eintippen. Dann **Übernehmen** – der Stream startet mit dem Ausschnitt neu.

- **Quellauflösung**: wie scharf das Handy aufnimmt. Bei kleinen Ausschnitten **Voll** wählen, damit der Ausschnitt nicht pixelig wird.
- **Ausgabebreite**: skaliert das Ergebnis auf eine feste Breite (Seitenverhältnis bleibt).
- Gespeichert in `alecast-settings.json` im ADB-Schlüssel-Ordner, bleibt also nach Neustart erhalten.
- Nach einer Änderung ändert sich die Auflösung des Streams → **Frigate einmal neu starten** und ggf. Bewegungsmasken neu zeichnen.

Das Bild ist eine Screenshot-Vorschau (ca. 1×/Sekunde), kein Video – gedacht zum Einstellen, nicht zum Zuschauen. **Nicht ins Internet freigeben**; von unterwegs nur über VPN/WireGuard öffnen oder `WEBUI_PASSWORD` setzen.

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `RTSP_URL` | – (Pflicht) | Ziel für den RTSP-Push |
| `ADB_SERIAL` | leer | Nur nötig bei mehreren Handys (`adb devices` zeigt die Seriennummer) |
| `MAX_SIZE` | `1280` | Längere Bildseite in Pixeln |
| `MAX_FPS` | `15` | Max. Bildrate vom Handy |
| `VIDEO_BIT_RATE` | `4000000` | Bitrate (bit/s) |
| `TRANSCODE` | `1` | `1` = neu kodieren mit fester Bildrate (stabil für Frigate), `0` = durchreichen (weniger CPU) |
| `OUT_FPS` | `10` | Feste Ausgabe-Bildrate bei `TRANSCODE=1` |
| `TAP_TEXT` | `Weiter ansehen\|Weiterschauen\|…` | Exakter Button-Text des Dialogs (Regex, mehrere mit `\|`) – leer = Auto-Tap aus |
| `TAP_INTERVAL` | `15` | Sekunden zwischen Dialog-Prüfungen |
| `CENTER_TAP_INTERVAL` | `600` | Tippt alle X Sekunden in die Bildschirmmitte (z. B. „Aktualisieren“ bei hängendem Feed), `0` = aus |
| `CENTER_X` / `CENTER_Y` | leer | Feste Tipp-Position statt automatisch ermittelter Mitte |
| `WEBUI_PORT` | `8080` | Port der Web-UI im Container |
| `WEBUI_PASSWORD` | leer | Optionales Passwort für die Web-UI (Benutzername beliebig) |
| `WEBUI_ENABLED` | `1` | `0` = Web-UI aus |
| `KEEP_AWAKE` | `1` | Display bei USB-Verbindung wach halten |

### Button-Text für Auto-Tap herausfinden

Wenn der Dialog auf dem Handy offen ist:

```bash
docker exec alecast sh -c "adb exec-out uiautomator dump /dev/tty | tr '>' '\n' | grep -o 'text=\"[^\"]*\"'"
```

Den angezeigten Text des Buttons als `TAP_TEXT` setzen.

## Fehlersuche

- **„Warte auf Handy …“ dauerhaft**: Kabel (Datenkabel, kein reines Ladekabel), USB-Debugging an? Auf dem Unraid `lsusb` – taucht das Handy auf?
- **Schwarzes Bild**: Display aus oder gesperrt → Displaysperre deaktivieren, „Aktiviert lassen“ einschalten.
- **Frigate meldet keine Frames**: `TRANSCODE=1` lassen; prüfen, ob in Frigate unter go2rtc der Stream `tapo_app` aktiv ist.
- **Nach Handy-Update keine Verbindung**: USB-Debugging-Freigabe erneut bestätigen.
