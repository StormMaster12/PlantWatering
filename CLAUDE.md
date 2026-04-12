# PlantWatering

Automated plant watering system for Raspberry Pi Zero 2W. Reads soil moisture via MCP3008 ADC, controls water pumps through GPIO relays, and serves a live log dashboard over HTTP.

Strict type checking should be followed. Make sure to make use of Pylance to ensure code qulaity is maintained.

Ensure all new code is covered with tests and try to maintain over 80% code coverage.

## Architecture

| File | Role |
|---|---|
| `plant_watering.py` | Main loop: polls sensors every 5 min, fires relays when moisture drops below threshold |
| `log_server.py` | Flask dashboard (port 5000) + TCP log receiver (port 9020) + email alerts |
| `config_manager.py` | Reads/writes `plants_config.json`; validates and mutates plant list |
| `plant_identifier.py` | Captures image from Pi Camera, sends to Claude API for plant ID + watering recommendations |
| `plant_types.py` | Shared `Plant` TypedDict |
| `plants_config.json` | Runtime config — up to 8 plants (one per MCP3008 channel) |
| `install-services.sh` | Deploys both systemd services to the Pi |

## Hardware

- **Raspberry Pi Zero 2W** running Raspberry Pi OS
- **MCP3008** 8-channel ADC via hardware SPI (CE0, GPIO 8) — channels 0–7
- **Capacitive moisture sensors** — raw ADC 0.55 = dry (0%), 0.20 = wet (100%)
- **Relay modules** (active-LOW by default) wired to BCM GPIO pins
- **Pi Camera Module** via CSI ribbon for plant identification

## Key constants (plant_watering.py)

```python
SENSOR_DRY     = 0.55   # raw ADC in open air → 0% moisture
SENSOR_WET     = 0.20   # raw ADC in water    → 100% moisture
POLL_INTERVAL  = 300    # seconds between moisture checks
MIN_WATER_GAP  = 3600   # minimum seconds between waterings per plant
```

## Plant config schema

```json
{
  "name": "string",
  "sensor_channel": 0,      // MCP3008 channel 0–7
  "relay_pin": null,        // BCM GPIO pin, null = plant skipped
  "threshold": 40,          // water when moisture % drops below this
  "water_duration": 3       // seconds to run pump per cycle
}
```

Plants with `relay_pin: null` are skipped gracefully at startup.

## Services

Two systemd services, both defined in `install-services.sh`:

- `plant-log-server.service` — starts first, holds the dashboard and log receiver
- `plant-watering.service` — depends on log server, runs the sensor/pump loop

Restart the watering service to pick up config changes:
```bash
sudo systemctl restart plant-watering.service
```

`config_manager.reload_watering_service()` does this programmatically (requires the sudoers rule installed by `install-services.sh`).

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `plant_identifier.py` | Claude API for plant identification |
| `PLANTS_CONFIG` | `config_manager.py` | Override config file path (default: `/home/pi/plant-watering/plants_config.json`) |
| `EMAIL_FROM/TO/PASSWORD/HOST/PORT` | `log_server.py` | SMTP alert emails on WARNING/ERROR |

## Dashboard API

- `GET  /` — live log dashboard (HTML)
- `GET  /api/logs` — JSON log buffer
- `GET  /api/plants` — list configured plants
- `POST /api/plants/add` — add a plant (validates, saves config, restarts service)
- `DELETE /api/plants/remove/<channel>` — remove plant by sensor channel
- `POST /api/identify` — trigger camera capture + Claude identification

## Development notes

- `plant_watering.py` uses `gpiozero` and `MCP3008` — these only work on the Pi. Dev/testing must mock or skip hardware.
- `plant_identifier.py` imports `picamera2` lazily (inside `capture_image()`) so it won't crash on non-Pi systems unless that function is called.
- The TCP log socket handler in `plant_watering.py` retries automatically if `log_server.py` isn't up yet.
- Config writes use a `.tmp` + atomic rename pattern to prevent corruption.
