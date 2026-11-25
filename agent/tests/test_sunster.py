import pytest
from diesel_heater_ble import ProtocolCBFF

from heater_agent.sunster import HeaterError, SunsterHeater, status_from_packet

from .fakes import (
    ADDRESS,
    REAL_COOLDOWN_36,
    REAL_HANDSHAKE_OK,
    REAL_STANDBY_10,
    REAL_VOLLGAS,
    SN,
    SimHeater,
    client_factory,
)


@pytest.fixture
def cbff():
    p = ProtocolCBFF()
    p.set_device_sn(SN)
    return p


def test_real_vollgas_packet(cbff):
    st = status_from_packet(cbff, bytes.fromhex(REAL_VOLLGAS))
    assert st is not None
    assert st.on and st.mode == "vollgas" and st.gear == 10
    assert st.phase == "heizen"
    assert st.set_temp is None
    assert 10 <= st.room_temp <= 20
    assert 12.0 <= st.voltage <= 14.0
    assert st.error_code == 0


def test_real_cooldown_packet(cbff):
    st = status_from_packet(cbff, bytes.fromhex(REAL_COOLDOWN_36))
    assert st is not None
    assert not st.on and st.cooling_down
    assert st.mode == "temperatur" and st.set_temp == 36


def test_real_standby_packet(cbff):
    st = status_from_packet(cbff, bytes.fromhex(REAL_STANDBY_10))
    assert st is not None
    assert not st.on and not st.cooling_down
    assert st.mode == "temperatur" and st.set_temp == 10


def test_handshake_reply_is_not_status(cbff):
    assert status_from_packet(cbff, bytes.fromhex(REAL_HANDSHAKE_OK)) is None
    assert cbff.v21_handshake_result(bytearray.fromhex(REAL_HANDSHAKE_OK)) is True


async def test_connect_start_vollgas_and_stop():
    sim = SimHeater()
    h = SunsterHeater(ADDRESS, 1234, client_factory(sim), timeout=1)
    await h.connect()
    assert h.connected and h.last_status.mode == "temperatur"

    st = await h.start_vollgas()
    assert st.on and st.mode == "vollgas" and st.gear == 10

    st = await h.stop()
    assert not st.on and st.cooling_down
    # Ausschalten muss den aktuellen Modus mitsenden (sonst wechselt die Heizung den Modus)
    assert sim.mode == 4


async def test_start_temperature():
    sim = SimHeater()
    h = SunsterHeater(ADDRESS, 1234, client_factory(sim), timeout=1)
    await h.connect()
    st = await h.start_temperature(36)
    assert st.on and st.mode == "temperatur" and st.set_temp == 36
    with pytest.raises(ValueError):
        await h.start_temperature(40)


async def test_wrong_pin():
    sim = SimHeater(pin=4321)
    h = SunsterHeater(ADDRESS, 1234, client_factory(sim), timeout=1)
    with pytest.raises(HeaterError, match="PIN"):
        await h.connect()
    assert not h.connected


async def test_pin_not_sent_in_plaintext_after_handshake():
    sim = SimHeater()
    h = SunsterHeater(ADDRESS, 1234, client_factory(sim), timeout=1)
    await h.connect()
    # Nur der erste Ping ist Klartext; alles danach ist verschlüsselt (kein FEAA-Header im Klartext)
    assert sim.writes[0][:2] == b"\xaa\x55"
    assert all(w[:2] != b"\xfe\xaa" for w in sim.writes[1:])
