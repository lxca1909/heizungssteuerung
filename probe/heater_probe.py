#!/usr/bin/env python3
"""Machbarkeitstest: Sunster-Dieselheizung per Bluetooth LE auslesen und schalten.

Befehle:
  scan                 Bluetooth-Geräte in der Nähe auflisten
  status ADRESSE       Verbinden und Status anzeigen (liest nur, schaltet nichts)
  on ADRESSE           Heizung einschalten, beobachten, nach --minutes automatisch aus
  off ADRESSE          Heizung ausschalten

Alle empfangenen Rohdaten werden in eine Logdatei geschrieben (ohne PIN),
damit wir bei Problemen das Protokoll analysieren können.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from diesel_heater_ble import (
    ProtocolAA55,
    ProtocolAA55Encrypted,
    ProtocolAA66,
    ProtocolAA66Encrypted,
    ProtocolCBFF,
    _decrypt_data,
)
from diesel_heater_ble.const import ERROR_NAMES, RUNNING_MODE_NAMES, RUNNING_STEP_NAMES

# (Service, Characteristic) – Sunster/Vevor nutzen meist FFE0/FFE1, manche FFF0/FFF1.
UUID_PAIRS = [
    ("0000ffe0-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb"),
    ("0000fff0-0000-1000-8000-00805f9b34fb", "0000fff1-0000-1000-8000-00805f9b34fb"),
]
KNOWN_SERVICES = {s for s, _ in UUID_PAIRS}

CMD_STATUS = 1
CMD_SET_MODE = 2  # Argument: MODE_LEVEL oder MODE_TEMPERATURE
CMD_POWER = 3
MODE_LEVEL = 1
MODE_TEMPERATURE = 2
MODE_VOLLGAS = 4  # Sunster V2.1, nicht in diesel-heater-ble
CMD_SET_TEMP = 4
CMD_SET_LEVEL = 5

HDR_AA55, HDR_AA66, HDR_AA77, HDR_CBFF = 0xAA55, 0xAA66, 0xAA77, 0xCBFF


class Heater:
    """Eine BLE-Sitzung mit einer Heizung inkl. Protokollerkennung."""

    def __init__(self, address: str, pin: int, log_dir: Path) -> None:
        self.address = address
        self.pin = pin
        self.client: BleakClient | None = None
        self.char: BleakGATTCharacteristic | None = None

        self.cbff = ProtocolCBFF()
        self.cbff.set_device_sn(address.replace(":", "").upper())
        self.protocol: Any = None  # erkanntes Protokoll
        self.state: dict[str, Any] = {}
        # Rohwerte aus dem Sunster-Status (Byte 11 = Modus, 12 = Wert, 13 = aktuelle Stufe).
        # Nötig, weil die Bibliothek Modus 4 (Vollgas) nicht kennt.
        self.cbff_mode: int | None = None
        self.cbff_param: int | None = None
        self.cbff_gear: int | None = None

        self.status_event = asyncio.Event()
        self.locked_event = asyncio.Event()  # AA77 = Sunster V2.1, PIN-Handshake nötig
        self.handshake_event = asyncio.Event()
        self.handshake_ok: bool | None = None

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"probe_{address.replace(':', '')}_{stamp}.log"
        self._log = self.log_path.open("a", encoding="utf-8")

    # ---------------------------------------------------------------- Logging
    def log(self, direction: str, text: str) -> None:
        self._log.write(f"{datetime.now().isoformat(timespec='milliseconds')} {direction} {text}\n")
        self._log.flush()

    # ------------------------------------------------------------ Verbindung
    async def connect(self) -> None:
        print(f"Verbinde mit {self.address} …")
        self.client = BleakClient(self.address, timeout=20.0)
        await self.client.connect()
        self.log("INFO", "verbunden")

        for svc in self.client.services:
            chars = ", ".join(f"{c.uuid[4:8]}{c.properties}" for c in svc.characteristics)
            self.log("GATT", f"service {svc.uuid}: {chars}")

        for svc_uuid, char_uuid in UUID_PAIRS:
            svc = self.client.services.get_service(svc_uuid)
            if svc and (c := svc.get_characteristic(char_uuid)):
                self.char = c
                break
        if self.char is None:
            services = [s.uuid for s in self.client.services]
            raise RuntimeError(
                "Keine bekannte Heizungs-Characteristic gefunden.\n"
                f"Vorhandene Services: {services}\n"
                f"Bitte die Logdatei schicken: {self.log_path}"
            )
        print(f"Characteristic gefunden: {self.char.uuid} {self.char.properties}")

        await self.client.start_notify(self.char, self._on_notify)
        await asyncio.sleep(0.3)

        # Wake-up / Statusabfrage im klassischen AA55-Format. Ältere Geräte
        # antworten mit AA55/AA66, Sunster V2.1 meldet sich mit AA77 (gesperrt).
        await self._write(ProtocolAA55().build_command(CMD_STATUS, 0, self.pin), "AA55-Status-Ping")
        await self._wait_any([self.status_event, self.locked_event], 6.0)

        if self.locked_event.is_set():
            print("Sunster V2.1 erkannt (verschlüsselt) – sende PIN-Handshake …")
            self.protocol = self.cbff
            self.cbff.set_v21_mode(True)
            await self._write(self.cbff.build_handshake(self.pin), "<Handshake, PIN ausgeblendet>")
            try:
                await asyncio.wait_for(self.handshake_event.wait(), 6.0)
            except asyncio.TimeoutError:
                raise RuntimeError("Keine Antwort auf den PIN-Handshake.") from None
            if not self.handshake_ok:
                raise RuntimeError("PIN wurde abgelehnt. Richtige PIN mit --pin angeben.")
            print("PIN akzeptiert.")
            await self.request_status()

        if not self.status_event.is_set():
            raise RuntimeError(
                "Heizung hat keinen verwertbaren Status geschickt.\n"
                f"Bitte die Logdatei schicken: {self.log_path}"
            )
        print(f"Protokoll: {self.protocol.name}")

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(self.char)
            except Exception:
                pass
            await self.client.disconnect()
        self.log("INFO", "getrennt")
        self._log.close()

    # ------------------------------------------------------------- Senden
    async def _write(self, packet: bytes | bytearray, label: str | None = None) -> None:
        assert self.client and self.char
        self.log("TX", label or "<Befehl, Inhalt ausgeblendet>")
        props = self.char.properties
        response = "write" in props and "write-without-response" not in props
        await self.client.write_gatt_char(self.char, bytes(packet), response=response)

    async def command(self, cmd: int, arg: int = 0) -> None:
        await self._write(self.protocol.build_command(cmd, arg, self.pin), f"cmd={cmd} arg={arg}")

    async def set_vollgas(self) -> None:
        """Sunster V2.1: Vollgas = Run-Befehl mit Modus 4, Wert 10 (schaltet auch ein).

        Ermittelt am 2026-11-25 aus dem Status, während Vollgas per App aktiv war:
        Byte 11 = 04, Byte 12 = 0a, Byte 13 = 0a.
        """
        if self.protocol is not self.cbff:
            raise RuntimeError("Vollgas ist nur für Sunster V2.1 (CBFF) bekannt.")
        packet = self.cbff._build_feaa(cmd_1=0x01, cmd_2=0x01,
                                       payload=bytes([MODE_VOLLGAS, 10, 0xFF, 0xFF]))
        await self._write(self.cbff._encrypt_cbff(packet, self.cbff._device_sn), "Vollgas (Modus 4, Wert 10)")

    async def request_status(self, timeout: float = 5.0) -> bool:
        self.status_event.clear()
        await self.command(CMD_STATUS)
        try:
            await asyncio.wait_for(self.status_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ----------------------------------------------------------- Empfangen
    def _on_notify(self, _char: Any, data: bytearray) -> None:
        self.log("RX", f"{len(data):3d} B {data.hex()}")
        header = (data[0] << 8) | data[1] if len(data) >= 2 else 0

        if header == HDR_AA77:
            self.locked_event.set()
            return

        parsed: dict[str, Any] | None = None
        cbff_frame = header == HDR_CBFF or self.cbff.is_feaa_frame(data)
        if self.protocol is self.cbff or cbff_frame:
            hs = self.cbff.v21_handshake_result(data)
            if hs is not None:
                self.handshake_ok = hs
                self.handshake_event.set()
                return
            if self.cbff.is_control_ack_only(data):
                self.log("INFO", "Befehl bestätigt (ACK)")
                return
            parsed = self.cbff.parse(data)
            if parsed is not None:
                self.protocol = self.cbff
                pkt = self.cbff._decode_feaa_packet(data)
                if pkt is not None and len(pkt) >= 46:
                    self.cbff_mode, self.cbff_param, self.cbff_gear = pkt[11], pkt[12], pkt[13]
        elif header == HDR_AA55 and len(data) in (18, 20):
            self.protocol = self.protocol or ProtocolAA55()
            parsed = self.protocol.parse(data)
        elif header == HDR_AA66 and len(data) == 20:
            self.protocol = self.protocol or ProtocolAA66()
            parsed = self.protocol.parse(data)
        elif len(data) == 48:
            dec = _decrypt_data(data)
            inner = (dec[0] << 8) | dec[1]
            if inner == HDR_AA55:
                self.protocol = self.protocol or ProtocolAA55Encrypted()
                parsed = self.protocol.parse(dec)
            elif inner == HDR_AA66:
                self.protocol = self.protocol or ProtocolAA66Encrypted()
                parsed = self.protocol.parse(dec)

        if parsed is None:
            self.log("INFO", "unbekanntes/ungültiges Paket")
            return
        if parsed.get("_cbff_data_suspect"):
            self.log("INFO", "CBFF-Daten unplausibel, verworfen")
            return
        self.state = parsed
        self.status_event.set()

    # -------------------------------------------------------------- Hilfen
    @staticmethod
    async def _wait_any(events: list[asyncio.Event], timeout: float) -> None:
        tasks = [asyncio.create_task(e.wait()) for e in events]
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()

    @property
    def is_on(self) -> bool:
        return bool(self.state.get("running_state"))

    @property
    def error_code(self) -> int:
        return int(self.state.get("error_code") or 0)

    def summary(self) -> str:
        s = self.state
        step = s.get("running_step")
        mode = s.get("running_mode")
        err = self.error_code
        parts = [
            f"{'AN ' if self.is_on else 'AUS'}",
            f"Phase={RUNNING_STEP_NAMES.get(step, step)}",
        ]
        if self.cbff_mode == MODE_VOLLGAS:
            parts.append(f"Modus=Vollgas | Stufe={self.cbff_gear}")
        else:
            parts.append(f"Modus={RUNNING_MODE_NAMES.get(mode, mode)}")
            if s.get("set_level") is not None:
                parts.append(f"Stufe={s['set_level']}")
            if s.get("set_temp") is not None:
                parts.append(f"Soll={s['set_temp']}°")
        for key, label, unit in (
            ("cab_temperature", "Raum", "°"),
            ("case_temperature", "Gehäuse", "°"),
            ("supply_voltage", "Spannung", "V"),
            ("co_ppm", "CO", "ppm"),
        ):
            if s.get(key) is not None:
                parts.append(f"{label}={s[key]}{unit}")
        parts.append(f"Fehler={err} ({ERROR_NAMES.get(err, '?')})" if err else "Fehler=keiner")
        return " | ".join(parts)


# ===================================================================== Befehle
async def cmd_scan(args: argparse.Namespace) -> int:
    print(f"Suche {args.timeout:.0f} s nach Bluetooth-Geräten …")
    found = await BleakScanner.discover(timeout=args.timeout, return_adv=True)
    rows = sorted(found.values(), key=lambda da: da[1].rssi, reverse=True)
    for dev, adv in rows:
        uuids = {u.lower() for u in adv.service_uuids}
        hint = "  <-- möglicherweise Heizung" if uuids & KNOWN_SERVICES else ""
        print(f"{dev.address}  RSSI {adv.rssi:4d}  {adv.local_name or dev.name or '-':24s}{hint}")
    print("\nTipp: Heizung kurz aus-/einstecken bzw. Handy-Bluetooth ausschalten –")
    print("viele Heizungen sind nur sichtbar, wenn gerade kein Handy verbunden ist.")
    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    h = Heater(args.address, args.pin, args.log_dir)
    loop = asyncio.get_running_loop()
    reading_notes = False
    try:
        await h.connect()
        print(h.summary())
        if args.watch and sys.stdin.isatty():
            print("Notiz ins Log: Text eintippen + Enter (z. B. 'Höhe auf 1500 m'). Beenden mit Strg+C.")
            loop.add_reader(sys.stdin.fileno(), _read_note, h)
            reading_notes = True
        while args.watch:
            await asyncio.sleep(args.interval)
            if await h.request_status():
                print(f"{datetime.now():%H:%M:%S}  {h.summary()}")
            else:
                print(f"{datetime.now():%H:%M:%S}  (keine Antwort)")
            if h.protocol is h.cbff:
                # Einstellungen mitlesen (reine Leseabfrage wie in der App), Antwort landet roh im Log
                await h._write(h.cbff.build_settings_query(), "Einstellungen lesen (03/00)")
                await asyncio.sleep(0.5)
        return 0
    finally:
        if reading_notes:
            loop.remove_reader(sys.stdin.fileno())
        await h.disconnect()
        print(f"Log: {h.log_path}")


def _read_note(h: Heater) -> None:
    """Eine eingetippte Zeile als NOTE ins Log schreiben (wird aufgerufen, wenn Enter gedrückt wurde)."""
    text = sys.stdin.readline().strip()
    if text:
        h.log("NOTE", text)
        print(f"{datetime.now():%H:%M:%S}  Notiz gespeichert: {text}")


def check_setting(h: Heater, args: argparse.Namespace) -> None:
    """Warnen, wenn die Heizung den gewünschten Modus/Wert nicht übernommen hat."""
    s = h.state
    if args.vollgas and h.cbff_mode != MODE_VOLLGAS:
        print(f"WARNUNG: Vollgas nicht übernommen – aktuell: {h.summary()}")
    if args.level is not None and (s.get("running_mode") != MODE_LEVEL or s.get("set_level") != args.level):
        print(f"WARNUNG: Stufe {args.level} nicht übernommen – aktuell: {h.summary()}")
    if args.temp is not None and (s.get("running_mode") != MODE_TEMPERATURE or s.get("set_temp") != args.temp):
        print(f"WARNUNG: Solltemperatur {args.temp}° nicht übernommen – aktuell: {h.summary()}")


async def turn_off(h: Heater) -> None:
    print("Schalte AUS …")
    await h.command(CMD_POWER, 0)
    for _ in range(10):
        await asyncio.sleep(2)
        if await h.request_status() and not h.is_on:
            print(f"Aus bestätigt: {h.summary()}")
            print("Hinweis: Die Heizung läuft jetzt einige Minuten nach (Abkühlphase). Strom NICHT trennen!")
            return
    print("WARNUNG: Ausschalten nicht bestätigt – bitte vor Ort / per App prüfen!")


async def cmd_on(args: argparse.Namespace) -> int:
    h = Heater(args.address, args.pin, args.log_dir)
    try:
        await h.connect()
        print(f"Aktuell: {h.summary()}")
        if h.error_code:
            print("Heizung meldet einen Fehler – Einschalten abgebrochen.")
            return 1
        if args.level is not None and h.protocol is h.cbff:
            # Real getestet (Sunster V2.1, Mainboard 1): Der Stufenbefehl landet als
            # Solltemperatur im Temperaturmodus (Stufe 10 -> 10 °C). Bis geklärt: gesperrt.
            print("--level wird bei dieser Heizung (Sunster V2.1) noch nicht unterstützt – "
                  "die Heizung würde die Stufe als Solltemperatur übernehmen. Abgebrochen.")
            return 1
        if not args.yes:
            hint = "bleibt AN bis du sie ausschaltest" if args.dauerhaft else f"automatisch AUS nach {args.minutes} min"
            answer = input(f"Heizung jetzt EINSCHALTEN ({hint})? [j/N] ")
            if answer.strip().lower() not in ("j", "ja", "y", "yes"):
                print("Abgebrochen.")
                return 1

        # Erst den Modus umschalten (Stufe bzw. Temperatur), dann den Wert setzen.
        # Nur den Wert zu setzen reicht nicht: Die Heizung bliebe sonst im alten
        # Modus, z. B. Temperaturbetrieb mit Sollwert 10 °C.
        if args.vollgas:
            await h.set_vollgas()
        elif args.temp is not None:
            await h.command(CMD_SET_MODE, MODE_TEMPERATURE)
            await asyncio.sleep(1)
            await h.command(CMD_SET_TEMP, args.temp)
        elif args.level is not None:
            await h.command(CMD_SET_MODE, MODE_LEVEL)
            await asyncio.sleep(1)
            await h.command(CMD_SET_LEVEL, args.level)
        await asyncio.sleep(1)
        await h.request_status()
        if not h.is_on:
            await h.command(CMD_POWER, 1)
            await asyncio.sleep(1)
            await h.request_status()
        check_setting(h, args)

        if args.dauerhaft:
            for _ in range(5):
                await asyncio.sleep(2)
                if await h.request_status() and h.is_on:
                    print(f"AN bestätigt: {h.summary()}")
                    print("Heizung läuft weiter. Ausschalten mit: heater_probe.py off ADRESSE (oder per App).")
                    return 0
            print("WARNUNG: Einschalten nicht bestätigt – bitte vor Ort / per App prüfen!")
            return 1

        deadline = time.monotonic() + args.minutes * 60
        print("Läuft. Strg+C schaltet sofort aus.")
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(args.interval)
                if await h.request_status():
                    print(f"{datetime.now():%H:%M:%S}  {h.summary()}")
                    if h.error_code:
                        print("Fehler gemeldet – schalte aus.")
                        break
                else:
                    print(f"{datetime.now():%H:%M:%S}  (keine Antwort)")
            else:
                print("Testzeit abgelaufen.")
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nAbbruch durch Benutzer.")
            # Abbruch "zurücknehmen", damit das Ausschalten sicher noch gesendet wird.
            task = asyncio.current_task()
            if task is not None:
                while task.uncancel():
                    pass
        await turn_off(h)
        return 0
    finally:
        await h.disconnect()
        print(f"Log: {h.log_path}")


async def cmd_off(args: argparse.Namespace) -> int:
    h = Heater(args.address, args.pin, args.log_dir)
    try:
        await h.connect()
        print(f"Aktuell: {h.summary()}")
        await turn_off(h)
        return 0
    finally:
        await h.disconnect()
        print(f"Log: {h.log_path}")


# ======================================================================= main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sunster-Heizung per BLE testen")
    p.add_argument("--pin", type=int, default=int(os.environ.get("SUNSTER_PIN", "1234")),
                   help="PIN der Heizung (Standard 1234 oder Umgebungsvariable SUNSTER_PIN)")
    p.add_argument("--log-dir", type=Path, default=Path("logs"), help="Ordner für Rohdaten-Logs")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="Geräte suchen")
    s.add_argument("--timeout", type=float, default=10.0)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("status", help="Status lesen (schaltet nichts)")
    s.add_argument("address")
    s.add_argument("--watch", action="store_true", help="fortlaufend anzeigen bis Strg+C")
    s.add_argument("--interval", type=float, default=5.0)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("on", help="einschalten, beobachten, automatisch ausschalten")
    s.add_argument("address")
    s.add_argument("--minutes", type=float, default=10.0, help="Testdauer bis Auto-Aus (Standard 10)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--level", type=int, choices=range(1, 11), help="Leistungsstufe 1–10")
    g.add_argument("--temp", type=int, choices=range(8, 37), metavar="8-36", help="Solltemperatur °C")
    g.add_argument("--vollgas", action="store_true", help="Vollgas-Modus (Sunster V2.1)")
    s.add_argument("--interval", type=float, default=5.0)
    s.add_argument("-y", "--yes", action="store_true", help="ohne Rückfrage")
    s.add_argument("--dauerhaft", action="store_true",
                   help="nur einschalten und Verbindung trennen – kein Auto-Aus")
    s.set_defaults(func=cmd_on)

    s = sub.add_parser("off", help="ausschalten")
    s.add_argument("address")
    s.set_defaults(func=cmd_off)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as err:  # verständliche Meldung statt Traceback
        print(f"FEHLER: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
