"""Simulierte Sunster-V2.1-Heizung auf GATT-Ebene (mit echter Verschlüsselung)."""
from __future__ import annotations

from diesel_heater_ble import ProtocolCBFF

from heater_agent.sunster import CHAR_UUID, SERVICE_UUID

ADDRESS = "54:6C:50:61:25:D9"
SN = ADDRESS.replace(":", "")

# Echte, verschlüsselte Pakete der Heizung (aufgezeichnet am 2026-11-25)
REAL_VOLLGAS = "bbff44306d5fc45572f8720d066e6233d44349465d4720f807380f859a08475029463357b8af7d7300ff0f659aba80"
REAL_COOLDOWN_36 = "bbff44306d5fc45572f8730b28656533d44341465d472001073607859a08475029463357b8af7d7300ff0f639ababe"
REAL_STANDBY_10 = "bbff44306d5fc45572f8710b06656133d44346465d4720fa075407859a08475029463357b8af7d7300ff0f639aba84"
REAL_AA77 = "aa770000000000000000"
REAL_HANDSHAKE_OK = "bbff4430485fc255723d"


class FakeChar:
    uuid = CHAR_UUID
    properties = ["read", "write", "notify"]


class FakeService:
    def __init__(self) -> None:
        self.char = FakeChar()

    def get_characteristic(self, uuid: str):
        return self.char if uuid == CHAR_UUID else None


class FakeServices:
    def __init__(self) -> None:
        self.svc = FakeService()

    def get_service(self, uuid: str):
        return self.svc if uuid == SERVICE_UUID else None


class SimHeater:
    """Verhalten wie real beobachtet: Modus 1 wird ignoriert, Start in Abkühlphase wirkungslos."""

    def __init__(self, pin: int = 1234) -> None:
        self.pin = pin
        self.run_state = 1  # 1 = aus/standby, 2 = an, 3 = Abkühlphase
        self.mode, self.value = 2, 10
        self.room = 7
        self.writes: list[bytes] = []

    def status_packet(self) -> bytes:
        p = ProtocolCBFF._build_feaa(0x80, 0x00, bytes(38))
        p[2] = 1
        p[8] = 1
        p[10] = self.run_state
        p[11], p[12] = self.mode, self.value
        p[13] = 10 if self.mode == 4 else 1
        p[14] = 3 if self.run_state == 2 else (4 if self.run_state == 3 else 0)
        p[18] = self.room
        p[-1] = sum(p[:-1]) & 0xFF
        return bytes(ProtocolCBFF._encrypt_cbff(p, SN))


class FakeClient:
    def __init__(self, sim: SimHeater, address: str, timeout: float = 20.0) -> None:
        self.sim = sim
        self.address = address
        self.is_connected = False
        self.services = FakeServices()
        self._cb = None
        self.authed = False

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(self, char, cb) -> None:
        self._cb = cb

    def _notify(self, data: bytes) -> None:
        self._cb(None, bytearray(data))

    async def write_gatt_char(self, char, data: bytes, response: bool) -> None:
        self.sim.writes.append(bytes(data))
        if data[:2] == b"\xaa\x55":
            self._notify(bytes.fromhex(REAL_AA77))
            return
        pkt = ProtocolCBFF._decrypt_cbff(bytearray(data), SN)
        assert ProtocolCBFF._has_valid_feaa_packet(pkt), pkt.hex()
        c1, c2 = pkt[6], pkt[7]
        if c1 == 0x06:
            self.authed = pkt[8] + 100 * pkt[9] == self.sim.pin
            reply = ProtocolCBFF._build_feaa(0x86, 0x00, bytes([1 if self.authed else 0]))
            self._notify(bytes(ProtocolCBFF._encrypt_cbff(reply, SN)))
            return
        assert self.authed, "Befehl ohne Authentifizierung"
        if c1 == 0x01:
            mode, value = pkt[8], pkt[9]
            if c2 == 1 and self.sim.run_state != 3:
                self.sim.run_state = 2
                if mode in (2, 4):  # Modus 1 übernimmt die echte Heizung nicht
                    self.sim.mode, self.sim.value = mode, value
                else:
                    self.sim.value = value
            elif c2 == 0 and self.sim.run_state == 2:
                self.sim.run_state = 3
        self._notify(self.sim.status_packet())


def client_factory(sim: SimHeater):
    return lambda address, timeout=20.0: FakeClient(sim, address, timeout)
