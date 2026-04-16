"""
Automated plant watering system
Raspberry Pi Zero 2W · MCP3008 ADC · gpiozero

Sensor calibration (from testing):
  0.55 = dry (air reading)   → 0% moisture
  0.20 = wet (water reading) → 100% moisture
"""

import logging
import logging.handlers
import signal
import sys
import time
import types
from dataclasses import dataclass, field
from typing import Optional

from gpiozero import MCP3008, OutputDevice
from config_manager import load_plants, load_sensor_calibration
from plant_types import Plant, Sensor

# ════════════════════════════════════════════════════════════════════
#  CALIBRATION  —  adjust if sensor readings drift over time
# ════════════════════════════════════════════════════════════════════

SENSOR_DRY = 0.55   # raw ADC value in open air  → 0% moisture
SENSOR_WET = 0.20   # raw ADC value in water      → 100% moisture

# ════════════════════════════════════════════════════════════════════
#  SYSTEM SETTINGS
# ════════════════════════════════════════════════════════════════════

POLL_INTERVAL   = 300   # seconds between moisture checks (5 min)
MIN_WATER_GAP   = 3600  # seconds minimum between waterings per plant (1 hr)
                        # prevents repeated triggering if soil drains slowly
LOG_LEVEL       = logging.INFO

# ════════════════════════════════════════════════════════════════════
#  PLANT CONFIGURATION  —  this is the only section you need to edit
#
#  To add a plant, copy one of the dict() entries and fill in:
#    name           : friendly label used in logs and alerts
#    sensor_channel : MCP3008 channel (0–7) the moisture sensor is on
#    relay_pin      : BCM GPIO pin number wired to the relay IN terminal
#                     set to None if not yet wired — plant is skipped safely
#    threshold      : moisture % below which watering is triggered (0–100)
#                     40 is a good default for most tropical houseplants
#                     20–25 suits cacti and succulents
#    water_duration : seconds to run the pump each watering cycle
#
#  Maximum 8 plants (one per MCP3008 channel).
# ════════════════════════════════════════════════════════════════════

PLANTS = [
    Plant(name="PlantControl 1", sensor_channel=0, relay_pin=None, threshold=40, water_duration=3),
    Plant(name="PlantControl 2", sensor_channel=1, relay_pin=None, threshold=40, water_duration=3),
    Plant(name="PlantControl 3", sensor_channel=2, relay_pin=None, threshold=40, water_duration=3),
    # ── add more below, up to channel 7 ──
    # dict(name="Cactus",   sensor_channel=3, relay_pin=None, threshold=20, water_duration=1),
    # dict(name="Fern",     sensor_channel=4, relay_pin=None, threshold=55, water_duration=4),
    # dict(name="Basil",    sensor_channel=5, relay_pin=None, threshold=45, water_duration=3),
    # dict(name="Snake",    sensor_channel=6, relay_pin=None, threshold=25, water_duration=2),
    # dict(name="Peace L.", sensor_channel=7, relay_pin=None, threshold=50, water_duration=3),
]

# ════════════════════════════════════════════════════════════════════
#  INTERNALS  —  no need to edit below this line
# ════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("plant_watering.log"),
        # Sends records to log_server.py over TCP.
        # SocketHandler retries automatically if the server isn't up yet.
        logging.handlers.SocketHandler(
            "localhost",
            logging.handlers.DEFAULT_TCP_LOGGING_PORT,  # 9020
        ),
    ],
)
log = logging.getLogger(__name__)


@dataclass
class PlantControl:
    name:           str
    sensor_channel: int
    relay_pin:      Optional[int]
    threshold:      float         # moisture % below which we water
    water_duration: float         # seconds to run pump per cycle

    # internal state — not set by caller
    _sensor:      Optional[MCP3008]       = field(default=None, init=False, repr=False)
    _pump:        Optional[OutputDevice]  = field(default=None, init=False, repr=False)
    last_watered: float                   = field(default=0.0,  init=False, repr=False)
    _sensor_max:  float                   = field(default=0.0,  init=False, repr=False)
    _sensor_min:  float                   = field(default=0.0,  init=False, repr=False)

    def setup(self, sensors: list[Sensor]) -> bool:
        """
        Initialise hardware. Returns True if the plant is ready to run.
        PlantControls with relay_pin=None are skipped gracefully.
        """
        if self.relay_pin is None:
            log.warning(
                "[%s] relay_pin not set — skipping (set relay_pin when wired)",
                self.name,
            )
            return False

        try:
            # MCP3008 uses hardware SPI by default (CE0, GPIO 8)
            self._sensor = MCP3008(channel=self.sensor_channel)
            sensor = next(
                (s for s in sensors if s["sensor_channel"] == self.sensor_channel), None
            )
            if sensor is None:
                raise ValueError(f"No calibration entry for channel {self.sensor_channel}")
            self._sensor_max = sensor["max"]
            self._sensor_min = sensor["min"]

            # Most relay modules are active-LOW: active_high=False means
            # calling .on() pulls the pin LOW and energises the relay.
            # If your relay board is active-HIGH, change to active_high=True.
            self._pump = OutputDevice(self.relay_pin, active_high=False, initial_value=False)

            log.info(
                "[%s] ready  |  ch=%d  gpio=%d  threshold=%d%%  duration=%ds",
                self.name, self.sensor_channel, self.relay_pin,
                self.threshold, self.water_duration,
            )
            return True

        except Exception as exc:
            log.error("[%s] setup failed: %s", self.name, exc)
            return False

    @property
    def moisture(self) -> float:
        """Current moisture reading as a percentage (0–100)."""
        if self._sensor is None:
            return 0.0
        return self.raw_to_moisture() # type: ignore

    def check_and_water(self) -> None:
        """Read moisture; water the plant if below threshold and cooldown has passed."""
        pump = self._pump
        if self._sensor is None or pump is None:
            return

        pct = self.moisture
        log.info("[%s] moisture: %.1f%%", self.name, pct)

        if pct >= self.threshold:
            return  # soil is moist enough

        now = time.monotonic()
        since_last = now - self.last_watered

        if since_last < MIN_WATER_GAP:
            log.warning(
                "[%s] below threshold (%.1f%%) but cooldown active — "
                "%.0f s remaining",
                self.name, pct, MIN_WATER_GAP - since_last,
            )
            return

        log.info(
            "[%s] moisture %.1f%% below threshold %d%% — watering for %ds",
            self.name, pct, self.threshold, self.water_duration,
        )
        pump.on()
        time.sleep(self.water_duration)
        pump.off()
        self.last_watered = time.monotonic()
        log.info("[%s] watering complete", self.name)

    def close(self) -> None:
        """Safely shut down hardware — always turns the pump off first."""
        if self._pump is not None:
            self._pump.off()
            self._pump.close()
        if self._sensor is not None:
            self._sensor.close()

    def raw_to_moisture(self) -> float:
        """
        Convert raw ADC voltage ratio to moisture percentage.

        The sensor outputs a LOWER voltage when WET and a HIGHER voltage
        when DRY, so the scale is inverted before converting to percent.

        Returns a value clamped between 0 and 100.
        """
        raw: float = self._sensor.value  # type: ignore[union-attr]
        moisture: float = (self._sensor_max - raw) / (self._sensor_max - self._sensor_min) * 100
        return max(0.0, min(100.0, moisture))


def build_plants() -> list[PlantControl]:
    """Load plants from plants_config.json and initialise hardware."""
    plants: list[PlantControl] = []
    sensors = load_sensor_calibration()
    
    for cfg in load_plants():
        plant = PlantControl(**cfg)
        if plant.setup(sensors):
            plants.append(plant)
    return plants


def main() -> None:
    log.info("═" * 60)
    log.info("PlantControl watering system starting")
    log.info("Poll interval: %ds  |  Min water gap: %ds", POLL_INTERVAL, MIN_WATER_GAP)
    log.info("Sensor range: %.2f (dry) → %.2f (wet)", SENSOR_DRY, SENSOR_WET)
    log.info("═" * 60)

    plants = build_plants()

    if not plants:
        log.error("No plants are ready (have you set relay_pin on each plant?)")
        sys.exit(1)

    log.info("%d plant(s) active", len(plants))

    # ── graceful shutdown on Ctrl-C or SIGTERM ──
    def shutdown(sig: int, _frame: types.FrameType | None) -> None:
        log.info("Shutdown signal received — turning off all pumps")
        for plant in plants:
            plant.close()
        log.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── main polling loop ──
    while True:
        log.info("── poll ────────────────────────────────────")
        for plant in plants:
            try:
                plant.check_and_water()
            except Exception as exc:
                # log but don't crash — keep other plants running
                log.error("[%s] unexpected error: %s", plant.name, exc)

        log.info("Next check in %ds", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
