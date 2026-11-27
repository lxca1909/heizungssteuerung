# Heizungssteuerung

Fernsteuerung für **Sunster-Dieselstandheizungen** über das Internet, statt nur per Bluetooth in Reichweite der Handy-App.

Gebaut für eine Motorradwerkstatt mit zwei Heizungen. Ein Mini-PC in der Werkstatt spricht per Bluetooth mit den Heizungen. Über einen WireGuard-Tunnel ist er mit einem Server verbunden, auf dem eine WebApp läuft.

<p align="center"><b>Vollgas oder Wunschtemperatur · Live-Status · automatische Abschaltung · Push aufs Handy</b></p>

## Funktionen

- **Steuern von überall:** Vollgas, Temperaturbetrieb (8–36 °C), Lüften (Stufe 1–10) und Aus.
- **Live-Status:** an/aus, Phase (Zündung, Heizen, Abkühlen), aktuelle Stufe, Raum- und Gehäusetemperatur, Spannung, Fehlercodes.
- **Laufzeitgrenze mit Verlängern:** Jeder Start läuft höchstens bis zur gewählten Laufzeit. 15 Minuten vorher kommt eine Push-Nachricht. Ein Tipp auf „+1 h“ oder „+2 h“ verlängert ohne Neustart.
- **Lokale Sicherheitsregeln:** Sie laufen auf dem Werkstatt-PC weiter, auch wenn Server oder Internet ausfallen (siehe [Sicherheit](#sicherheit)).
- **Verlauf und Protokoll:** Temperaturkurve, Ereignisse, wer wann geschaltet hat.
- **Push-Nachrichten** über [ntfy](https://ntfy.sh): Fehler, Sicherheitsabschaltung, Werkstatt-PC offline, Erinnerung vor der Abschaltung.
- **Handy-tauglich:** als App installierbar (PWA), heller und dunkler Modus.

## Aufbau

```
 [Heizung 1]  [Heizung 2]
       \  Bluetooth LE  /
  ┌──────────────────────────────┐        WireGuard         ┌──────────────────────────────────┐
  │ Mini-PC Werkstatt            │ ───────────────────────▶ │ Rootserver                       │
  │  heater-agent (Docker)       │        MQTT              │  Mosquitto (nur im VPN)          │
  │  · Bluetooth-Steuerung       │                          │  WebApp (FastAPI) ◀── Caddy/HTTPS│
  │  · Sicherheitsregeln         │                          │  · Login, Live-Status, Verlauf   │
  └──────────────────────────────┘                          └──────────────────────────────────┘
                                                                         ▲
                                                               Handy / Browser
```

| Ordner | Inhalt | Läuft auf |
|---|---|---|
| [`probe/`](probe/) | Testskript: Heizung per Bluetooth suchen, auslesen, schalten | Laptop/PC mit Bluetooth |
| [`agent/`](agent/) | `heater-agent`: Bluetooth ↔ MQTT, Sicherheitsregeln | Mini-PC Werkstatt |
| [`server/`](server/) | WebApp: Login, Status, Steuerung, Verlauf, Push | Rootserver |
| [`deploy/`](deploy/) | Docker Compose für den Server (Mosquitto + WebApp), Caddy-Vorlage | Rootserver |
| [`deploy/werkstatt/`](deploy/werkstatt/) | Docker Compose für den Agenten, WireGuard-Vorlage | Mini-PC Werkstatt |

## Schnellstart

1. **Testen, ob deine Heizung unterstützt wird:** siehe [`probe/README.md`](probe/README.md).
   ```bash
   cd probe && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
   python heater_probe.py scan
   python heater_probe.py status AA:BB:CC:DD:EE:FF
   ```
2. **Server einrichten:** siehe [`deploy/README.md`](deploy/README.md). Dort kommen `./setup.sh`, der Login, `docker compose up -d` und der Caddy-Block.
3. **Mini-PC einrichten:** siehe [`deploy/werkstatt/README.md`](deploy/werkstatt/README.md). Dort kommen WireGuard, `.env` und `docker compose up -d`.

## Sicherheit

Eine Dieselheizung ist eine Feuerstätte. Wer sie aus der Ferne startet, ist nicht vor Ort, wenn etwas schiefgeht. Deshalb setzt der Agent lokal durch:

| Regel | Standard |
|---|---|
| Laufzeitgrenze pro Start/Verlängerung (auch bei Start per App) | 120 min, max. 240 min |
| Erinnerung vor der Abschaltung | 15 min vorher |
| Kein Start bei Fehlercode oder in der Abkühlphase | immer |
| Vollgas aus, wenn der Raum zu warm ist | 30 °C |
| CO-Abschaltung (optional, mit Sensor per MQTT oder CO-Wert der Heizung) | ab 50 ppm, Freigabe unter 20 ppm |

Außerdem gilt:
- **Ein geprüfter CO-Melder (EN 50291) und ein Rauchmelder gehören in jeden Raum mit Dieselheizung.** Diese Software ersetzt sie nicht.
- **Die PIN der Heizung in der Sunster-App ändern.** Ab Werk ist sie meist `1234`, und jeder in Bluetooth-Reichweite kennt sie.
- Der MQTT-Broker ist nur im WireGuard-Netz erreichbar, die WebApp nur über HTTPS mit Login.

## Protokoll (Sunster V2.1)

Die Bluetooth-Kommunikation baut auf der Bibliothek [`diesel-heater-ble`](https://github.com/Spettacolo83/diesel-heater-ble) auf. Mit einer echten Heizung (Mainboard-Typ 1) habe ich zusätzlich Folgendes herausgefunden:

- Service `FFE0`, Characteristic `FFE1` (write + notify).
- Ein Klartext-Ping wird mit `AA77` beantwortet („gesperrt“). Danach folgen ein verschlüsselter PIN-Handshake und verschlüsselte `FEAA`-Befehle.
- Status (FEAA `80/00`, 47 Byte, alle Mehrbyte-Werte little-endian):

  | Byte | Bedeutung |
  |---|---|
  | 8 | Mainboard-Typ, `0` = Steuerung schläft (Messwerte ungültig) |
  | 10 | Laufzustand: `0` ganz aus, `1` Standby, `2` an, `3` Abkühlphase (~4:20 min) |
  | 11 | Modus: `2` Temperatur, `3` Lüften, **`4` Vollgas** (nicht in der Bibliothek) |
  | 12 | Wert: Solltemperatur 8–36 °C bzw. Lüfterstufe 1–10 bzw. 10 |
  | 13 | aktuelle Stufe 1–10 |
  | 14 | Phase: `0` Standby, `1` Selbsttest, `2` Zündung (~2,5 min), `3` Heizen, `4` Abkühlen |
  | 18/19 | Raumtemperatur °C |
  | 23/24 | Spannung × 10 |
  | 25/26 | Gehäusetemperatur × 10 |
  | 27/28 | CO, `FFFF` = kein Sensor |

- **Modus setzen:** Run-Befehl `01/01` mit Payload `[Modus, Wert, FF, FF]`, z. B. Vollgas `[04, 0A, FF, FF]`. Mit `01/00` und dem aktuellen Modus wird ausgeschaltet.
- Am Bedienteil wechseln die Modi der Reihe nach: Temperatur → Lüften → Vollgas. Den Stufenmodus (`01`) gibt es bei dieser Heizung nicht. Sie übernimmt die Stufe stattdessen als Solltemperatur.
- Während der Abkühlphase werden Startbefehle ignoriert.

Andere Sunster-Modelle können abweichen. Das Testskript schreibt alle Rohdaten in `probe/logs/`, damit sie sich analysieren lassen.

## Entwicklung

```bash
cd agent  && pip install -e ".[dev]" && pytest     # Protokoll, Sicherheitsregeln, Agent
cd server && pip install -r requirements.txt pytest pytest-asyncio && pytest
```
Die Protokolltests nutzen echte, aufgezeichnete Pakete einer Sunster-Heizung. Die übrigen Tests laufen gegen eine simulierte Heizung mit echter Verschlüsselung.

## Hinweis

Privates Bastelprojekt, nicht mit Sunster verbunden oder von Sunster unterstützt. Nutzung auf eigene Gefahr.
