# Mini-PC in der Werkstatt einrichten

Auf dem Mini-PC laufen:
- **WireGuard** direkt auf dem System. Der Tunnel zum Rootserver baut sich beim Booten auf.
- **heater-agent** in Docker. Er steuert die Heizungen per Bluetooth und spricht über den Tunnel mit dem Server.
- Optional ein normaler Desktop mit Browser für Reparaturanleitungen.

## 1. Betriebssystem

Empfehlung: **Debian 13** (oder Ubuntu 24.04 LTS). Wenn der PC auch für Anleitungen
genutzt wird, eine Desktop-Umgebung mit installieren (z. B. Xfce, ressourcenschonend).

Im BIOS einstellen:
- **„Restore on AC Power Loss“ → Power On.** Nach einem Stromausfall startet der PC dann von selbst.
- Energiesparmodi für USB/Bluetooth ggf. deaktivieren

## 2. Pakete

```bash
sudo apt update
sudo apt install -y bluez wireguard-tools git
# Docker nach https://docs.docker.com/engine/install/debian/
sudo systemctl enable --now bluetooth docker
```

Bluetooth prüfen: `bluetoothctl show` sollte `Powered: yes` zeigen.

## 3. WireGuard

```bash
wg genkey | sudo tee /etc/wireguard/privatekey | wg pubkey | sudo tee /etc/wireguard/publickey
sudo cp wg0.conf.example /etc/wireguard/wg0.conf     # anpassen, siehe Kommentare
sudo chmod 600 /etc/wireguard/wg0.conf /etc/wireguard/privatekey
sudo systemctl enable --now wg-quick@wg0
ping 10.8.0.1                                         # Rootserver über den Tunnel
```
Den Public Key des Mini-PCs auf dem Rootserver als Peer eintragen (siehe Ende von `wg0.conf.example`).

## 4. heater-agent

```bash
git clone https://github.com/lxca1909/heizungssteuerung.git /opt/heizungssteuerung
cd /opt/heizungssteuerung/deploy/werkstatt
cp ../../agent/config.example.yaml config.yaml        # Heizungen, MQTT-Host = WireGuard-IP des Servers
cat > config.local.yaml <<'EOF'
mqtt:
  username: werkstatt
  password: "<Passwort aus setup.sh auf dem Server>"
EOF
chmod 600 config.local.yaml
docker compose up -d --build
docker compose logs -f
```

Im Log sollten `MQTT verbunden` und `… verbunden` für jede Heizung erscheinen.

## Hinweise

- **Bluetooth-Reichweite:** Den Mini-PC möglichst nah an die Heizungen stellen. Bei schwachem Empfang hilft ein
  USB-Bluetooth-Stick mit Antenne an einem Verlängerungskabel. Die Signalstärke zeigt
  `python probe/heater_probe.py scan`.
- **Solange der Agent läuft, kann sich die Sunster-App nicht verbinden.** Zum Arbeiten mit der App den Agenten kurz anhalten:
  `docker compose stop`, danach `docker compose start`.
- Updates: `git pull && docker compose up -d --build`
