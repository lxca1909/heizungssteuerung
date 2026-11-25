# Heizungssteuerung

Fernsteuerung für zwei Sunster-Dieselstandheizungen in einer Motorradwerkstatt.

```
[Heizung 1] [Heizung 2]         [ESP32-Sensoren: Temp, CO2, CO]
      \  BLE  /                       | WLAN / MQTT
   [Werkstatt-PC: heater-agent] ------+
      | WireGuard
   [Rootserver: MQTT + FastAPI-WebApp]  <-- Handy / Browser
```

| Ordner | Inhalt |
|---|---|
| `probe/` | Machbarkeitstest: Heizung per Bluetooth auslesen und schalten |
| `agent/` | Dienst für den Werkstatt-PC: BLE ↔ MQTT, lokale Sicherheitsregeln |

## Erkenntnisse zum Protokoll (Sunster V2.1)

- BLE-Service `FFE0`, Characteristic `FFE1` (write + notify)
- Gesperrt (`AA77`) → verschlüsselter PIN-Handshake, danach verschlüsselte FEAA-Befehle
  (Bibliothek [`diesel-heater-ble`](https://github.com/Spettacolo83/diesel-heater-ble))
- Status Byte 11 = Modus: `02` Temperatur, `04` Vollgas (nicht in der Bibliothek)
- Vollgas setzen: Run-Befehl mit Modus 4, Wert 10
- Stufenmodus (`01`) wird von dieser Heizung nicht übernommen
- Während der Abkühlphase nimmt die Heizung keinen Startbefehl an
