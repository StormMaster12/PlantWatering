"""
config_manager.py
Shared module for reading and writing plants_config.json.
Used by both plant_watering.py and log_server.py.
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)

CONFIG_PATH = Path(os.getenv("PLANTS_CONFIG", "/home/pi/plant-watering/plants_config.json"))
_lock = Lock()

MAX_PLANTS   = 8
MAX_CHANNELS = 8   # MCP3008 channel count


# ── Read ────────────────────────────────────────────────────────────

def load_plants() -> list[dict]:
    """Return the list of plant dicts from config, or [] on error."""
    try:
        with _lock:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        return data.get("plants", [])
    except FileNotFoundError:
        log.warning("plants_config.json not found — returning empty list")
        return []
    except json.JSONDecodeError as exc:
        log.error("plants_config.json is malformed: %s", exc)
        return []


# ── Write ───────────────────────────────────────────────────────────

def save_plants(plants: list[dict]) -> None:
    """Overwrite the config file with the given plant list."""
    payload = {"plants": plants}
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with _lock:
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(CONFIG_PATH)   # atomic on Linux
    log.info("plants_config.json saved (%d plant(s))", len(plants))


# ── Add ─────────────────────────────────────────────────────────────

class ConfigError(Exception):
    pass


def add_plant(
    name:           str,
    sensor_channel: int,
    relay_pin:      Optional[int],
    threshold:      int,
    water_duration: int,
) -> dict:
    """
    Validate and append a new plant to the config.
    Returns the new plant dict.
    Raises ConfigError if validation fails.
    """
    plants = load_plants()

    # Validation
    if len(plants) >= MAX_PLANTS:
        raise ConfigError(f"Maximum of {MAX_PLANTS} plants already configured")

    if not 0 <= sensor_channel <= MAX_CHANNELS - 1:
        raise ConfigError(f"sensor_channel must be 0–{MAX_CHANNELS - 1}")

    used_channels = {p["sensor_channel"] for p in plants}
    if sensor_channel in used_channels:
        raise ConfigError(f"Sensor channel {sensor_channel} is already in use")

    if not 0 <= threshold <= 100:
        raise ConfigError("threshold must be between 0 and 100")

    if not 1 <= water_duration <= 30:
        raise ConfigError("water_duration must be between 1 and 30 seconds")

    plant = {
        "name":           name.strip(),
        "sensor_channel": sensor_channel,
        "relay_pin":      relay_pin,
        "threshold":      threshold,
        "water_duration": water_duration,
    }

    plants.append(plant)
    save_plants(plants)

    log.info("Added plant: %s (ch=%d, relay=%s)", name, sensor_channel, relay_pin)
    return plant


def remove_plant(sensor_channel: int) -> bool:
    """Remove a plant by its sensor channel. Returns True if removed."""
    plants = load_plants()
    new_plants = [p for p in plants if p["sensor_channel"] != sensor_channel]
    if len(new_plants) == len(plants):
        return False
    save_plants(new_plants)
    return True


# ── Reload watering service ─────────────────────────────────────────

def reload_watering_service() -> tuple[bool, str]:
    """
    Restart the plant-watering systemd service so it picks up the new config.
    Returns (success, message).
    """
    try:
        subprocess.run(
            ["sudo", "systemctl", "restart", "plant-watering.service"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        log.info("plant-watering.service restarted")
        return True, "Watering service restarted successfully"
    except subprocess.CalledProcessError as exc:
        msg = f"Failed to restart service: {exc.stderr.decode().strip()}"
        log.error(msg)
        return False, msg
    except Exception as exc:
        msg = f"Unexpected error restarting service: {exc}"
        log.error(msg)
        return False, msg
