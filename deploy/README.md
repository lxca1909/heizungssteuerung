# Deployment auf dem Rootserver

Hier laufen der MQTT-Broker (Mosquitto) und die WebApp. Der Broker ist nur über WireGuard erreichbar, die WebApp nur über deinen bestehenden Caddy.

## 1. Repo holen und einrichten

```bash
git clone git@github.com:lxca1909/heizungssteuerung.git
cd heizungssteuerung/deploy
./setup.sh
```

`setup.sh` erledigt Folgendes:
- legt `.env` an,
- erzeugt `SECRET_KEY` und die MQTT-Passwörter,
- gibt **einmalig** das MQTT-Passwort für den Werkstatt-PC aus. Bitte notieren.

## 2. Login für die WebApp anlegen

```bash
docker compose build app
docker compose run --rm --no-deps app python -m app.hashpw <name>
```
Die ausgegebene Zeile `USERS=<name>:scrypt.…` in `.env` eintragen. Für mehrere Benutzer die Einträge mit Komma trennen.

## 3. `.env` prüfen

- `WG_IP`: die WireGuard-IP des Servers, zum Beispiel `10.8.0.1` (`ip -4 addr show wg0`).
- `NTFY_URL` (optional): Push aufs Handy über die ntfy-App, zum Beispiel `https://ntfy.sh/<langes-zufälliges-wort>`.
  Das Thema ist öffentlich lesbar, wenn man den Namen kennt. Deshalb einen langen Zufallsnamen wählen oder ntfy selbst hosten.

## 4. Starten

```bash
docker compose up -d --build
docker compose logs -f
```

Hinweis: Der Broker bindet sich an `WG_IP`. WireGuard muss also vor Docker oben sein. Falls der Container nach einem Neustart nicht startet, hilft `docker compose up -d`. Eine dauerhafte Lösung ist, `docker.service` nach `wg-quick@wg0.service` starten zu lassen.

## 5. Caddy

Den Block aus `Caddyfile.snippet` in deinen Caddyfile übernehmen. Dann im DNS einen A/AAAA-Eintrag für `heizung.example.com` anlegen und Caddy neu laden.

## 6. Werkstatt-PC verbinden

Auf dem Werkstatt-PC (siehe `agent/README.md`) in `agent/config.yaml` als MQTT-Host die WireGuard-IP des Servers eintragen.
In `agent/config.local.yaml` kommen die Zugangsdaten:
```yaml
mqtt:
  username: werkstatt
  password: "<Passwort aus setup.sh>"
```

## Ports im Überblick

| Dienst | Erreichbar über |
|---|---|
| MQTT 1883 | nur `WG_IP` (WireGuard) |
| WebApp 8080 | nur `127.0.0.1`, davor Caddy mit HTTPS |

## Backup

Verlauf, Alarme und Befehlsprotokoll liegen im Docker-Volume `app-data` (SQLite).
Daten älter als 90 Tage werden automatisch gelöscht.
