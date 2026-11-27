# Machbarkeitstest: Sunster-Heizung per Bluetooth

Ziel: prüfen, ob ein Linux-Rechner deine Sunster-Heizungen ohne die Handy-App
auslesen und schalten kann. Basis ist die Bibliothek
[`diesel-heater-ble`](https://github.com/Spettacolo83/diesel-heater-ble).

## Vorbereitung (einmalig, auf dem Laptop)

```bash
cd heizungssteuerung/probe
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Bluetooth muss an sein (`bluetoothctl show` → `Powered: yes`).

**Wichtig:** Die Heizung akzeptiert meist nur **eine** Bluetooth-Verbindung.
Während des Tests auf dem Handy die Sunster-App schließen oder Bluetooth ausschalten.

## Ablauf in der Werkstatt

**1. Heizungen finden**
```bash
python heater_probe.py scan
```
Notiere die Adressen (Format `AA:BB:CC:DD:EE:FF`). Heizungen sind oft mit
„möglicherweise Heizung“ markiert. Ein RSSI-Wert näher an 0 bedeutet bessere
Verbindung, unter etwa −85 wird es wackelig. Notiere die Werte auch für die
spätere Platzierung des Werkstatt-PCs.

**2. Status lesen (schaltet nichts)**
```bash
python heater_probe.py --pin 1234 status AA:BB:CC:DD:EE:FF
python heater_probe.py --pin 1234 status AA:BB:CC:DD:EE:FF --watch   # fortlaufend, Strg+C beendet
```
Die PIN ist die gleiche wie in der Sunster-App (ab Werk meist `1234`).
Vergleiche Raumtemperatur und Spannung mit dem Display bzw. der App.

**Einstellungen erforschen:** `--watch` laufen lassen und am Bedienteil der Heizung eine Einstellung nach der anderen ändern.
Nach jeder Änderung eintippen, was du geändert hast, und Enter drücken, zum Beispiel `Höhe auf 1500 m`. Die Notiz landet mit Zeitstempel im Log.
So lässt sich später zuordnen, welches Byte zu welcher Einstellung gehört.
Das Skript liest bei jeder Runde zusätzlich die Einstellungen der Heizung aus. Das ist eine reine Leseabfrage.
Die Sunster-App kann sich währenddessen nicht verbinden, deshalb das Bedienteil benutzen.

**3. Einschalten testen** (nur wenn du vor Ort bist)
```bash
python heater_probe.py --pin 1234 on AA:BB:CC:DD:EE:FF --minutes 10
python heater_probe.py --pin 1234 on AA:BB:CC:DD:EE:FF --level 3      # mit Leistungsstufe
python heater_probe.py --pin 1234 on AA:BB:CC:DD:EE:FF --temp 18      # mit Solltemperatur
```
**Einfach nur einschalten** (bleibt an, wie mit der App):
```bash
python heater_probe.py --pin 1234 on AA:BB:CC:DD:EE:FF --dauerhaft
```

**Vollgas** (Sunster V2.1, Modus 4, ermittelt aus dem Heizungsstatus):
```bash
python heater_probe.py --pin 1234 on AA:BB:CC:DD:EE:FF --vollgas --dauerhaft
```
`--level` ist bei Sunster V2.1 gesperrt, weil die Heizung die Stufe als
Solltemperatur übernimmt.

Ohne `--dauerhaft`: Fragt vorher nach, zeigt alle 5 s den Status und schaltet nach Ablauf der Zeit
oder bei **Strg+C** automatisch wieder aus. Bei einer Fehlermeldung der
Heizung schaltet es sofort aus.

**4. Notfalls ausschalten**
```bash
python heater_probe.py --pin 1234 off AA:BB:CC:DD:EE:FF
```

Danach läuft die Heizung einige Minuten nach (Abkühlphase). In dieser Zeit
den Strom nicht trennen.

## Was ich danach von dir brauche

- Hat `status` funktioniert? Welches **Protokoll** wurde angezeigt?
- Stimmen die Werte mit der App bzw. dem Display überein?
- Hat An/Aus funktioniert?
- Die Dateien aus dem Ordner `logs/`, vor allem wenn etwas nicht klappt.
  Die PIN wird nicht mitgeloggt.
- Die RSSI-Werte aus dem Scan, von der Stelle aus gemessen, an der der PC später stehen soll.

## Häufige Probleme

| Meldung | Ursache / Lösung |
|---|---|
| Gerät taucht im Scan nicht auf | Handy-App noch verbunden, oder Heizung zu weit weg |
| `PIN wurde abgelehnt` | richtige PIN mit `--pin` angeben |
| `Keine bekannte Heizungs-Characteristic` | anderes Protokoll, Logdatei schicken |
| `keinen verwertbaren Status` | Logdatei schicken, dann analysieren wir die Rohdaten |
| Verbindungsabbrüche | näher ran oder USB-Bluetooth-Stick mit Antenne verwenden |
