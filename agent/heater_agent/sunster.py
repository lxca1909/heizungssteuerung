"""BLE-Anbindung an Sunster-V2.1-Heizungen (CBFF/FEAA, verschlüsselt).

Real getestet am 2026-11-25 (Mainboard-Typ 1):
- Service FFE0 / Characteristic FFE1 (write + notify)
- Klartext-Ping -> Heizung antwortet AA77 (gesperrt) -> verschlüsselter PIN-Handshake
- Status: FEAA 80/00, 47 Byte. Byte 8 = Mainboard-Typ (0 = Heizung schläft, Werte
  ungültig), 10 = Laufzustand (0 ganz aus, 1 Standby, 2 an, 3 Abkühlen), 11 = Modus,
  12 = Wert, 13 = aktuelle Stufe 1–10, 14 = Phase, 18/19 = Raum °C, 23/24 = Spannung x10,
  25/26 = Gehäuse °C x10 (jeweils little-endian), 27/28 = CO (FFFF = kein Sensor)
- Run-Befehl FEAA 01/xx mit Payload [Modus, Wert, FF, FF]; xx = 1 an, 0 aus
- Modus 2 = Temperatur (Wert = Soll 8–36 °C), 3 = Lüften (Wert = Lüfterstufe 1–10),
  4 = Vollgas (Wert 10). Am Bedienteil: Temperatur -> Lüften -> Vollgas
- Modus 1 (Stufe) gibt es bei dieser Heizung nicht
- Laufzustand 3 = Abkühlphase (~4:20 min): Startbefehle werden ignoriert
- Kein CO-Sensor (co_ppm immer None)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable

from bleak import BleakClient
from diesel_heater_ble import ProtocolAA55, ProtocolCBFF

log = logging.getLogger(__name__)

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

MODE_TEMPERATURE = 2
MODE_VENTILATION = 3
MODE_VOLLGAS = 4
VOLLGAS_VALUE = 10
MIN_TEMP, MAX_TEMP = 8, 36
MIN_FAN, MAX_FAN = 1, 10

RUN_STATE_COOLDOWN = 3

MODE_NAMES = {1: "stufe", 2: "temperatur", 3: "lueften", 4: "vollgas"}
PHASE_NAMES = {0: "standby", 1: "selbsttest", 2: "zuendung", 3: "heizen", 4: "abkuehlen", 6: "lueften"}

_HDR_AA77 = b"\xaa\x77"


class HeaterError(Exception):
    """Kommunikationsfehler mit der Heizung."""


@dataclass(frozen=True)
class HeaterStatus:
    on: bool
    run_state: int
    phase: str
    mode: str
    raw_mode: int
    raw_value: int
    set_temp: int | None
    fan_level: int | None       # nur im Lüftmodus
    gear: int | None            # aktuelle Stufe 1–10 (Byte 13)
    room_temp: float | None
    case_temp: float | None
    voltage: float | None
    co_ppm: float | None
    error_code: int

    @property
    def cooling_down(self) -> bool:
        return self.run_state == RUN_STATE_COOLDOWN

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cooling_down"] = self.cooling_down
        return d

    @property
    def asleep(self) -> bool:
        """Heizung ganz aus: Messwerte sind dann ungültig."""
        return self.room_temp is None and self.voltage is None


def status_from_packet(cbff: ProtocolCBFF, data: bytes | bytearray) -> HeaterStatus | None:
    """Ein (verschlüsseltes) Statuspaket in HeaterStatus übersetzen, sonst None."""
    pkt = cbff._decode_feaa_packet(bytearray(data))
    if pkt is None or len(pkt) < 46 or pkt[6] not in (0x80, 0x81):
        return None
    if pkt[6] == 0x80 and pkt[7] != 0x00:
        return None
    parsed = cbff.parse(bytearray(data))
    if parsed is None or parsed.get("_cbff_data_suspect"):
        return None
    raw_mode, raw_value, gear, step = pkt[11], pkt[12], pkt[13], pkt[14]
    # Schläft die Steuerung (Byte 8 = 0), sind die Messwerte Müll (z. B. 0,0 V).
    asleep = pkt[8] == 0 or not parsed.get("supply_voltage")
    def measured(key: str) -> float | None:
        return None if asleep else parsed.get(key)
    return HeaterStatus(
        on=bool(parsed.get("running_state")),
        run_state=pkt[10],
        phase=PHASE_NAMES.get(step, f"phase_{step}"),
        mode=MODE_NAMES.get(raw_mode, f"modus_{raw_mode}"),
        raw_mode=raw_mode,
        raw_value=raw_value,
        set_temp=raw_value if raw_mode == MODE_TEMPERATURE else None,
        fan_level=raw_value if raw_mode == MODE_VENTILATION else None,
        gear=None if asleep else gear,
        room_temp=measured("cab_temperature"),
        case_temp=measured("case_temperature"),
        voltage=measured("supply_voltage"),
        co_ppm=parsed.get("co_ppm"),
        error_code=int(parsed.get("error_code") or 0),
    )


class SunsterHeater:
    """Eine BLE-Verbindung zu einer Sunster-V2.1-Heizung.

    Alle öffentlichen Methoden sind über ein Lock serialisiert.
    """

    def __init__(
        self,
        address: str,
        pin: int,
        client_factory: Callable[..., Any] = BleakClient,
        timeout: float = 6.0,
    ) -> None:
        self.address = address
        self._pin = pin
        self._client_factory = client_factory
        self._timeout = timeout
        self._client: Any = None
        self._char: Any = None
        self._lock = asyncio.Lock()

        self._cbff = ProtocolCBFF()
        self._cbff.set_device_sn(address.replace(":", "").upper())
        self._locked = asyncio.Event()
        self._handshake = asyncio.Event()
        self._handshake_ok = False
        self._status_event = asyncio.Event()
        self.last_status: HeaterStatus | None = None

    # ------------------------------------------------------------ Verbindung
    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def connect(self) -> None:
        async with self._lock:
            if self.connected:
                return
            self._locked.clear()
            self._handshake.clear()
            self._status_event.clear()
            client = self._client_factory(self.address, timeout=20.0)
            await client.connect()
            self._client = client
            try:
                svc = client.services.get_service(SERVICE_UUID)
                self._char = svc.get_characteristic(CHAR_UUID) if svc else None
                if self._char is None:
                    raise HeaterError("Characteristic FFE1 nicht gefunden")
                await client.start_notify(self._char, self._on_notify)
                await asyncio.sleep(0.3)

                # Klartext-Ping: V2.1-Heizungen antworten mit AA77 (gesperrt).
                await self._write(ProtocolAA55().build_command(1, 0, self._pin))
                await self._wait(self._locked, "keine AA77-Antwort (keine Sunster-V2.1-Heizung?)")
                self._cbff.set_v21_mode(True)
                await self._write(self._cbff.build_handshake(self._pin))
                await self._wait(self._handshake, "keine Antwort auf PIN-Handshake")
                if not self._handshake_ok:
                    raise HeaterError("PIN abgelehnt")
                await self._request_status()
            except BaseException:
                await self._disconnect_unlocked()
                raise
            log.info("%s verbunden", self.address)

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect_unlocked()

    async def _disconnect_unlocked(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as err:  # pragma: no cover - nur Logging
                log.debug("disconnect: %s", err)

    # ------------------------------------------------------------- Befehle
    async def status(self) -> HeaterStatus:
        async with self._lock:
            return await self._request_status()

    async def start_vollgas(self) -> HeaterStatus:
        return await self._run(MODE_VOLLGAS, VOLLGAS_VALUE, power=True)

    async def start_temperature(self, temp: int) -> HeaterStatus:
        if not MIN_TEMP <= temp <= MAX_TEMP:
            raise ValueError(f"Temperatur muss zwischen {MIN_TEMP} und {MAX_TEMP} °C liegen")
        return await self._run(MODE_TEMPERATURE, temp, power=True)

    async def set_ventilation(self, level: int) -> HeaterStatus:
        """Lüften (nur Gebläse) mit Stufe 1–10. Wie in der Sunster-App nur bei laufender Heizung."""
        if not MIN_FAN <= level <= MAX_FAN:
            raise ValueError(f"Lüfterstufe muss zwischen {MIN_FAN} und {MAX_FAN} liegen")
        if not (self.last_status and self.last_status.on):
            raise HeaterError("Lüften geht nur, wenn die Heizung läuft")
        return await self._run(MODE_VENTILATION, level, power=True)

    async def stop(self) -> HeaterStatus:
        last = self.last_status
        known = (MODE_TEMPERATURE, MODE_VENTILATION, MODE_VOLLGAS)
        mode = last.raw_mode if last and last.raw_mode in known else MODE_TEMPERATURE
        value = last.raw_value if last and mode == last.raw_mode else 20
        return await self._run(mode, value, power=False)

    async def _run(self, mode: int, value: int, power: bool) -> HeaterStatus:
        async with self._lock:
            self._ensure_connected()
            packet = self._cbff._build_feaa(0x01, 1 if power else 0, bytes([mode, value, 0xFF, 0xFF]))
            await self._write(self._cbff._encrypt_cbff(packet, self._cbff._device_sn))
            await asyncio.sleep(1.0)
            return await self._request_status()

    # ------------------------------------------------------------ Intern
    def _ensure_connected(self) -> None:
        if not self.connected:
            raise HeaterError("nicht verbunden")

    async def _request_status(self) -> HeaterStatus:
        self._ensure_connected()
        self._status_event.clear()
        await self._write(self._cbff.build_command(1, 0, self._pin))
        await self._wait(self._status_event, "keine Statusantwort")
        assert self.last_status is not None
        return self.last_status

    async def _write(self, packet: bytes | bytearray) -> None:
        props = self._char.properties
        response = "write" in props and "write-without-response" not in props
        await self._client.write_gatt_char(self._char, bytes(packet), response=response)

    async def _wait(self, event: asyncio.Event, message: str) -> None:
        try:
            await asyncio.wait_for(event.wait(), self._timeout)
        except asyncio.TimeoutError:
            raise HeaterError(message) from None

    def _on_notify(self, _char: Any, data: bytearray) -> None:
        if data[:2] == _HDR_AA77:
            self._locked.set()
            return
        hs = self._cbff.v21_handshake_result(data)
        if hs is not None:
            self._handshake_ok = hs
            self._handshake.set()
            return
        status = status_from_packet(self._cbff, data)
        if status is not None:
            self.last_status = status
            self._status_event.set()
