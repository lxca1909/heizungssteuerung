"""Lokale Sicherheitsregeln. Laufen auf dem Werkstatt-PC, auch ohne Server.

Reine Logik ohne BLE/MQTT, damit sie vollständig testbar ist.

Regeln:
- Jeder Start hat eine Laufzeitgrenze (max. ``max_runtime_min``). Danach AUS.
  Auch per App/Bedienteil gestartete Heizungen bekommen diese Grenze ab dem
  Zeitpunkt, an dem der Agent sie laufend sieht. Die Laufzeit lässt sich ohne
  Neustart verlängern (Totmannschaltung: ein Mensch bestätigt regelmäßig).
- CO-Alarm (Sensor oder Heizung meldet >= ``co_off_ppm``): alle Heizungen AUS,
  Starts gesperrt, bis CO wieder unter ``co_clear_ppm`` liegt.
- Ist ein CO-Sensor Pflicht (``co_sensor_required``), sind Starts gesperrt,
  solange keine frischen Messwerte da sind.
- Fehlercode der Heizung: keine Starts, bis der Fehler weg ist.
- Abkühlphase: Starts werden abgelehnt (die Heizung ignoriert sie ohnehin).
- Vollgas: AUS, wenn der Raum ``vollgas_max_room_temp`` erreicht.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .sunster import HeaterStatus


@dataclass
class SafetyConfig:
    max_runtime_min: int = 240
    default_runtime_min: int = 120
    co_off_ppm: float = 50.0
    co_clear_ppm: float = 20.0
    co_sensor_required: bool = False
    co_sensor_max_age_s: float = 180.0
    vollgas_max_room_temp: float | None = 30.0
    reminder_min: float = 15.0  # so lange vor der Abschaltung eine Erinnerung senden (0 = aus)


@dataclass
class _Run:
    deadline: float


@dataclass
class Safety:
    cfg: SafetyConfig = field(default_factory=SafetyConfig)
    co_alarm: bool = False
    _co_ppm: float | None = None
    _co_ts: float | None = None
    _runs: dict[str, _Run] = field(default_factory=dict)

    # ------------------------------------------------------------- CO
    def update_co(self, ppm: float, now: float) -> bool:
        """Neuen CO-Messwert verarbeiten. Gibt True zurück, wenn der Alarm NEU auslöst."""
        self._co_ppm, self._co_ts = ppm, now
        return self._eval_co(ppm)

    def _eval_co(self, ppm: float) -> bool:
        if ppm >= self.cfg.co_off_ppm and not self.co_alarm:
            self.co_alarm = True
            return True
        if self.co_alarm and ppm < self.cfg.co_clear_ppm:
            self.co_alarm = False
        return False

    def co_sensor_fresh(self, now: float) -> bool:
        return self._co_ts is not None and now - self._co_ts <= self.cfg.co_sensor_max_age_s

    @property
    def co_ppm(self) -> float | None:
        return self._co_ppm

    # ----------------------------------------------------------- Start
    def runtime_for(self, minutes: float | None) -> float:
        if minutes is None:
            minutes = self.cfg.default_runtime_min
        if minutes <= 0:
            raise ValueError("Laufzeit muss positiv sein")
        return min(float(minutes), float(self.cfg.max_runtime_min))

    def check_start(self, status: HeaterStatus | None, now: float) -> str | None:
        """None = Start erlaubt, sonst Begründung der Ablehnung."""
        if self.co_alarm:
            return "CO-Alarm aktiv"
        if self.cfg.co_sensor_required and not self.co_sensor_fresh(now):
            return "keine aktuellen Werte vom CO-Sensor"
        if status is None:
            return "Heizung nicht verbunden"
        if status.error_code:
            return f"Heizung meldet Fehler {status.error_code}"
        if status.cooling_down:
            return "Heizung kühlt noch ab, bitte kurz warten"
        return None

    def on_started(self, heater_id: str, minutes: float, now: float) -> float:
        deadline = now + minutes * 60
        self._runs[heater_id] = _Run(deadline)
        return deadline

    def extend(self, heater_id: str, minutes: float, now: float) -> float:
        """Laufzeit einer laufenden Heizung verlängern (ohne Neustart).

        Die Grenze ``max_runtime_min`` gilt ab jetzt, nicht ab dem Start: Wer
        regelmäßig bestätigt, kann beliebig lange heizen.
        """
        run = self._runs.get(heater_id)
        if run is None:
            raise ValueError("Heizung läuft nicht")
        if minutes <= 0:
            raise ValueError("Verlängerung muss positiv sein")
        new = max(run.deadline, now) + minutes * 60
        run.deadline = min(new, now + self.cfg.max_runtime_min * 60)
        return run.deadline

    def on_stopped(self, heater_id: str) -> None:
        self._runs.pop(heater_id, None)

    def deadline(self, heater_id: str) -> float | None:
        run = self._runs.get(heater_id)
        return run.deadline if run else None

    # ---------------------------------------------------------- Laufend
    def check_running(self, heater_id: str, status: HeaterStatus, now: float) -> str | None:
        """Für eine laufende Heizung prüfen, ob sie AUS muss. None = weiterlaufen."""
        if status.co_ppm is not None and status.co_ppm > 0:
            # Heizungen mit eigenem CO-Sensor melden hier echte Werte.
            self._eval_co(max(status.co_ppm, self._co_ppm or 0.0))

        if not status.on:
            self.on_stopped(heater_id)
            return None

        if heater_id not in self._runs:
            # Extern gestartet (App/Bedienteil): Standard-Laufzeitgrenze ab jetzt.
            self.on_started(heater_id, self.runtime_for(None), now)

        if self.co_alarm:
            return "CO-Alarm"
        if now >= self._runs[heater_id].deadline:
            return "Laufzeit abgelaufen"
        limit = self.cfg.vollgas_max_room_temp
        if (
            limit is not None
            and status.mode == "vollgas"
            and status.room_temp is not None
            and status.room_temp >= limit
        ):
            return f"Raumtemperatur {status.room_temp:.0f} °C erreicht (Vollgas-Grenze {limit:.0f} °C)"
        return None
