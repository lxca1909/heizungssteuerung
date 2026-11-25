#!/usr/bin/env bash
# Einmalige Einrichtung auf dem Rootserver: .env + MQTT-Passwörter erzeugen.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env

rand() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-"$1"; }
setvar() { # setvar NAME WERT – nur setzen, wenn noch leer
  if grep -qE "^$1=$" .env; then sed -i "s|^$1=$|$1=$2|" .env; fi
}

setvar SECRET_KEY "$(rand 64)"
setvar MQTT_PASSWORD "$(rand 32)"
WEBAPP_PW=$(grep -E '^MQTT_PASSWORD=' .env | cut -d= -f2-)

if [ ! -s mosquitto/passwd ]; then
  WERKSTATT_PW=$(rand 32)
  SENSOR_PW=$(rand 32)
  docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 sh -c "
    rm -f /m/passwd && touch /m/passwd && chown 1883:1883 /m/passwd && chmod 0700 /m/passwd &&
    mosquitto_passwd -b /m/passwd webapp '$WEBAPP_PW' &&
    mosquitto_passwd -b /m/passwd werkstatt '$WERKSTATT_PW' &&
    mosquitto_passwd -b /m/passwd sensor '$SENSOR_PW'" 2>&1 | grep -vE '^(Adding password|Warning: File|To fix this)' || true
  echo
  echo "MQTT-Zugang für den Werkstatt-PC (in agent/config.local.yaml eintragen):"
  echo "  mqtt:"
  echo "    username: werkstatt"
  echo "    password: $WERKSTATT_PW"
  echo
  echo "MQTT-Zugang für den ESP32-Sensor (später): sensor / $SENSOR_PW"
  echo "(Diese Passwörter werden nur jetzt angezeigt – bitte notieren.)"
fi

echo
if ! grep -qE '^USERS=.+' .env; then
  echo "Noch zu tun: Login für die WebApp anlegen und die ausgegebene USERS=...-Zeile in .env eintragen:"
  echo "  docker compose build app && docker compose run --rm --no-deps app python -m app.hashpw luca"
fi
echo "Dann WG_IP in .env prüfen und starten: docker compose up -d --build"
