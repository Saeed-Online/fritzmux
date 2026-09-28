# FritzMux

**RTSP → HTTP IPTV Relay für AVM Fritzbox** (DVB-C) mit bis zu 4 parallelen Streams, EPG-Unterstützung und Logo-Proxy.

Fritzbox generiert M3U-Playlisten mit `rtsp://` URLs – moderne IPTV Player können damit nichts anfangen.
FritzMux importiert die Playlist, wandelt RTSP-Streams auf HTTP um und stellt EPG-Daten als XMLTV bereit.

## Features

- **Fritzbox-Scanner** – durchsucht Fritzbox nach M3U-Pfaden
- **M3U Import** – per URL, Datei-Upload oder Fritzbox-Scan; mehrere Playlisten zusammenführbar (Duplikaterkennung)
- **On-Demand Streaming** – ffmpeg relayt RTSP → HTTP (stream copy, max. 4 parallele Streams)
- **EPG Proxy** – mehrere XMLTV-Quellen (auch `.xml.gz`) → gemergte XMLTV, automatischer Background-Refresh
- **Logo Proxy** – AVM-Logos per Knopfdruck, externe Logos gecached, Einzel-Upload
- **Web UI** – Kanalverwaltung (editieren, löschen, gruppieren, EPG-Zuordnung, Logo)
- **Docker** – Daten liegen außerhalb des Git-Repos, Datenverlust durch `git pull` ausgeschlossen

## Quickstart

```bash
git clone https://github.com/ElectronicResearch/fritzmux.git
cd fritzmux
docker compose up -d
```

`http://<docker-host-ip>:8181` → Fritzbox scannen → Playlist-URL im IPTV Player eintragen.

> **network_mode: host** wird verwendet, damit der Container auf Geräte im Heimnetz zugreifen kann.

## IPTV Player Einrichtung

1. **Playlist:** `http://<fritzmux-ip>:8181/api/channels.m3u`
   - Zeigt ein Player kein Bild (iPhone/Apple TV, viele Handy-Apps): HLS-Variante
     `http://<fritzmux-ip>:8181/api/channels.m3u?format=hls` – Video unverändert, Ton als AAC,
     ca. 5 s Startzeit. Nutzt denselben Tuner wie der normale Stream.
2. **EPG:** `http://<fritzmux-ip>:8181/api/epg.xml`

### Xtream-Codes-Login (IPTV Smarters Pro auf LG/Samsung u.a.)

Apps, die nur einen Xtream-Login anbieten:

| Feld       | Wert                              |
| ---------- | --------------------------------- |
| Name       | beliebig                          |
| Username   | beliebig (oder `FRITZMUX_XTREAM_USER`)     |
| Password   | beliebig (oder `FRITZMUX_XTREAM_PASSWORD`) |
| URL        | `http://<fritzmux-ip>:8181`       |

Unterstützt: `player_api.php` (Login, Live-Kategorien, Live-Sender, Kurz-EPG), `get.php`, `xmltv.php`,
Streams unter `/live/<user>/<pass>/<id>.ts` bzw. `.m3u8` (HLS mit AAC-Ton). Kein VOD/Serien.

## Web UI

### M3U Import

| Methode | Beschreibung |
|---|---|
| **Fritzbox scannen** | Durchsucht Fritzbox nach M3U-Pfaden (`/dvb/m3u/tvhd.m3u`, `/dvb/m3u/tvsd.m3u`, TR-064, Legacy) |
| **Von URL importieren** | M3U von beliebiger URL |
| **Datei-Upload** | M3U-Datei von Festplatte hochladen (Download aus Fritzbox-Webinterface) |

**Checkbox "Vorhandene Kanäle ersetzen"**: Ohne Haken werden neue Kanäle angehängt (Duplikate erkannt), mit Haken werden alle bisherigen gelöscht.

### EPG Quellen

1. Name + URL eingeben → **Hinzufügen** (unterstützt `.xml` und `.xml.gz`)
2. **EPG jetzt aktualisieren** – manueller Refresh
3. Automatischer Background-Refresh alle 60 Minuten
4. Quellen werden in `data/epg_sources.json` gespeichert und bleiben nach Neustart erhalten

### Logos

- **AVM Logos laden** – Holt passende Kanallogos von `https://download.avm.de/tv/logos/` (matched anhand Kanalname)
- **Einzel-Upload** – Im Edit-Modal pro Kanal: Logo-Datei hochladen
- **Logo-URL** – Externes Logo per URL setzen (wird gecached)

### Kanalverwaltung

- **Gruppenansicht** – Kanäle werden nach Gruppe sortiert
- **Filter/Suche** – nach Name oder Gruppe filtern
- **Multi-Select** – mehrere Kanäle auswählen, löschen oder "Nur Auswahl behalten"
- **Edit-Modal** – Kanal bearbeiten: Titel, tvg-id, tvg-name, Gruppe, RTSP-URL, Logo, EPG-Zuordnung

## Architektur

```
Fritzbox (RTSP + DVB-C Tuner)
       │
       ▼
   FritzMux (Port 8181, host network)
       │
       ├── M3U Rewrite   → http://app:8181/api/channels.m3u
       ├── EPG Proxy     → http://app:8181/api/epg.xml
       ├── Logo Proxy    → http://app:8181/api/logo/{id}
       └── Stream Relay  → http://app:8181/stream/{id}
                                │
                                ▼
                         IPTV Player (TiviMate, …)
```

## API Endpoints

| Endpoint | Methode | Beschreibung |
|---|---|---|
| `/` | GET | Web UI |
| `/api/status` | GET | Server-Status |
| `/api/channels` | GET | Kanalliste als JSON |
| `/api/channels/{id}` | GET/PUT/DELETE | Kanal Detail/Bearbeiten/Löschen |
| `/api/channels/clear` | POST | Alle Kanäle löschen |
| `/api/channels.m3u` | GET | **Generierte M3U** für TiviMate |
| `/api/import/url` | POST | M3U von URL importieren |
| `/api/import/upload` | POST | M3U-Datei hochladen |
| `/api/scan/fritzbox` | POST | Fritzbox nach M3U durchsuchen |
| `/api/epg.xml` | GET | **XMLTV EPG** für TiviMate |
| `/api/epg/refresh` | GET | EPG manuell neu laden |
| `/api/epg/source` | POST | EPG-Quelle hinzufügen |
| `/api/epg/sources` | GET | EPG-Quellen auflisten |
| `/api/epg/channels` | GET | EPG-Kanäle (aus XMLTV) |
| `/api/logos/avm` | POST | AVM-Logos abrufen |
| `/api/logo/{id}` | GET | Gecachtes Kanallogo |
| `/api/logo/{id}/upload` | POST | Logo hochladen |
| `/stream/{id}` | GET | **Live-Stream** (ffmpeg relayt RTSP → MPEG-TS) |

## Konfiguration

Per Umgebungsvariable (z.B. in `docker-compose.yml` unter `environment:`):

| Variable                        | Standard    | Beschreibung                                                        |
| ------------------------------- | ----------- | ------------------------------------------------------------------- |
| `FRITZMUX_MAX_STREAMS`          | 4           | Maximale parallele Sender (Tuner der Fritzbox)                      |
| `FRITZMUX_STREAM_TIMEOUT`       | 15          | Sekunden, die ein Sender nach dem letzten Zuschauer weiterläuft      |
| `FRITZMUX_STREAM_START_TIMEOUT` | 10          | Sekunden bis zum ersten Bild, sonst Fehler 502                      |
| `FRITZMUX_RTSP_TRANSPORT`       | udp         | RTSP-Transport zur Fritzbox (TCP lehnt die Box mit 461 ab)          |
| `FRITZMUX_TRACKS`               | main        | `main`: erstes Video + erste Tonspur (nötig für LG/webOS-Player, sonst Standbild nach ~10 s). `all`: alle Tonspuren + Videotext/Untertitel (VLC, Kodi, TiviMate) |
| `FRITZMUX_VIEWER_BUFFER_MB`     | 64          | Puffer pro Zuschauer, bevor ein nicht mehr lesender Player getrennt wird |
| `FRITZMUX_EPG_INTERVAL`         | 3600        | EPG-Aktualisierung in Sekunden                                      |
| `FRITZMUX_EPG_KEEP_PAST_HOURS`  | 6           | Wie lange beendete Sendungen im EPG bleiben                         |
| `FRITZMUX_USER` / `FRITZMUX_PASSWORD` | leer  | Optionaler Passwortschutz (HTTP Basic) für Web UI und Admin-API. Playlist, EPG, Logos und Streams bleiben für Player offen. |
| `FRITZMUX_DATA_DIR`             | /app/data   | Datenverzeichnis                                                    |

Ein Sender, der gerade niemanden mehr hat, gibt seinen Tuner sofort frei, wenn ein anderer Sender ihn braucht (Zappen).
Mehrere Geräte können denselben Sender gleichzeitig schauen und teilen sich dabei einen Tuner.

## Docker

```yaml
services:
  fritzmux:
    build: .
    container_name: fritzmux
    network_mode: host
    volumes:
      - ../fritzmux_data:/app/data
    restart: unless-stopped
```

Daten (Kanäle, EPG-Cache, Logos) liegen in `../fritzmux_data/` – außerhalb des Git-Repos und damit sicher vor `git pull` / `git reset`.

## Development

```bash
# Mit Hot-Reload
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8181
```
