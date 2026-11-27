# heater-agent (Werkstatt-PC)

Läuft dauerhaft auf dem Werkstatt-PC und hat drei Aufgaben:
- die Bluetooth-Verbindung zu den Heizungen halten,
- die Sicherheitsregeln lokal durchsetzen,
- Zustand und Befehle per MQTT mit dem Server austauschen.

Die Sicherheitsregeln greifen auch, wenn der Server nicht erreichbar ist.

## Installation

```bash
cd agent
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.yaml config.yaml      # anpassen
.venv/bin/heater-agent -c config.yaml -v
```

Als Dienst: `heater-agent.service` nach `/etc/systemd/system/` kopieren und die Pfade/den Benutzer anpassen.
Dann `systemctl enable --now heater-agent` ausführen. Der Benutzer braucht Zugriff auf Bluetooth (Gruppe `bluetooth`).

**Hinweis:** Solange der Agent verbunden ist, kann sich die Sunster-App nicht verbinden. Die Heizung akzeptiert nur eine Verbindung.

## MQTT-Schnittstelle

Präfix `werkstatt` (konfigurierbar):

| Thema | Richtung | Inhalt |
|---|---|---|
| `werkstatt/agent/status` | Agent → | `online` / `offline` (retained, Last Will) |
| `werkstatt/heater/<id>/state` | Agent → | JSON-Zustand (retained) |
| `werkstatt/heater/<id>/cmd` | → Agent | Befehl (JSON) |
| `werkstatt/heater/<id>/ack` | Agent → | `{"id", "ok", "message"}` |
| `werkstatt/alarm` | Agent → | `{"level", "heater", "message", "ts"}` |

Befehle:
```json
{"id": "abc", "action": "start", "mode": "vollgas", "minutes": 120, "ts": 1764090000}
{"id": "abc", "action": "start", "mode": "temperatur", "temp": 22, "minutes": 120}
{"id": "abc", "action": "extend", "minutes": 60}
{"id": "abc", "action": "stop"}
{"action": "refresh"}
```
Ist `ts` gesetzt und der Befehl älter als 60 s, wird er ignoriert.

## Sicherheitsregeln (`heater_agent/safety.py`)

- Jeder Start hat eine Laufzeitgrenze (Standard 120 min, maximal 240 min). Das gilt auch für Starts per App.
  15 min vorher kommt eine Erinnerung (`alarm` mit `level: reminder`). Mit `extend` lässt sich ohne Neustart verlängern; die 240 min gelten ab dem Zeitpunkt der Verlängerung.
- CO ab 50 ppm schaltet alle Heizungen AUS. Starts sind gesperrt, bis der Wert unter 20 ppm fällt.
- Optional: kein Start ohne frische Werte vom CO-Sensor (`co_sensor_required`).
- Kein Start bei Fehlercode oder während der Abkühlphase.
- Vollgas geht AUS, sobald der Raum 30 °C erreicht.

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
Die Protokolltests verwenden echte, aufgezeichnete Pakete der Heizung.
