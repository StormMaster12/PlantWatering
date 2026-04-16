"""Tests for plant_watering.py."""

import time
from unittest.mock import MagicMock, patch

import pytest

import plant_watering
from plant_watering import PlantControl, build_plants
from plant_types import Plant, Sensor

_DEFAULT_SENSORS: list[Sensor] = [
    {"sensor_channel": 0, "max": 0.55, "min": 0.20},
]


def _setup_plant(
    plant: PlantControl,
    sensors: list[Sensor],
    sensor_value: float = 0.375,
) -> tuple[MagicMock, MagicMock]:
    """Call setup() with mocked hardware; return (mock_sensor, mock_pump)."""
    mock_sensor = MagicMock()
    mock_sensor.value = sensor_value
    mock_pump = MagicMock()
    with (
        patch("plant_watering.MCP3008", return_value=mock_sensor),
        patch("plant_watering.OutputDevice", return_value=mock_pump),
    ):
        plant.setup(sensors)
    return mock_sensor, mock_pump


# ── PlantControl.raw_to_moisture ──────────────────────────────────────


class TestRawToMoisture:
    def _plant(self, raw: float) -> PlantControl:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        _setup_plant(plant, _DEFAULT_SENSORS, sensor_value=raw)
        return plant

    def test_dry_calibration_point_returns_zero(self) -> None:
        assert self._plant(0.55).raw_to_moisture() == pytest.approx(0.0)  # type: ignore[arg-type]

    def test_wet_calibration_point_returns_100(self) -> None:
        assert self._plant(0.20).raw_to_moisture() == pytest.approx(100.0)  # type: ignore[arg-type]

    def test_midpoint_returns_50(self) -> None:
        assert self._plant(0.375).raw_to_moisture() == pytest.approx(50.0)  # type: ignore[arg-type]

    def test_clamps_below_zero_for_very_dry_reading(self) -> None:
        assert self._plant(0.70).raw_to_moisture() == 0.0

    def test_clamps_above_100_for_very_wet_reading(self) -> None:
        assert self._plant(0.05).raw_to_moisture() == 100.0

    def test_quarter_moisture(self) -> None:
        # raw 0.4625 = 0.55 - 0.25*(0.55-0.20) → 25%
        assert self._plant(0.4625).raw_to_moisture() == pytest.approx(25.0)  # type: ignore[arg-type]


# ── PlantControl.setup ────────────────────────────────────────────────


class TestPlantControlSetup:
    def test_skips_and_returns_false_without_relay_pin(self) -> None:
        plant = PlantControl(
            name="No Relay", sensor_channel=0, relay_pin=None, threshold=40, water_duration=3
        )
        assert plant.setup(_DEFAULT_SENSORS) is False
        assert plant._sensor is None  # type: ignore[reportPrivateUsage]
        assert plant._pump is None  # type: ignore[reportPrivateUsage]

    def test_initialises_hardware_and_returns_true(self) -> None:
        sensors: list[Sensor] = [{"sensor_channel": 2, "max": 0.55, "min": 0.20}]
        plant = PlantControl(
            name="Wired", sensor_channel=2, relay_pin=17, threshold=40, water_duration=3
        )
        with (
            patch("plant_watering.MCP3008") as mock_mcp,
            patch("plant_watering.OutputDevice") as mock_od,
        ):
            result = plant.setup(sensors)

        assert result is True
        mock_mcp.assert_called_once_with(channel=2)
        mock_od.assert_called_once_with(17, active_high=False, initial_value=False)
        assert plant._sensor is mock_mcp.return_value  # type: ignore[reportPrivateUsage]
        assert plant._pump is mock_od.return_value  # type: ignore[reportPrivateUsage]

    def test_sensor_calibration_loaded_from_sensors_list(self) -> None:
        sensors: list[Sensor] = [{"sensor_channel": 0, "max": 0.60, "min": 0.15}]
        plant = PlantControl(
            name="Cal", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        mock_sensor = MagicMock()
        mock_sensor.value = 0.375  # midpoint of 0.15–0.60 → 50%
        with (
            patch("plant_watering.MCP3008", return_value=mock_sensor),
            patch("plant_watering.OutputDevice"),
        ):
            plant.setup(sensors)

        assert plant.moisture == pytest.approx(50.0)  # type: ignore[arg-type]

    def test_returns_false_when_sensor_channel_not_in_calibration(self) -> None:
        plant = PlantControl(
            name="Uncalibrated", sensor_channel=7, relay_pin=17, threshold=40, water_duration=3
        )
        with (
            patch("plant_watering.MCP3008"),
            patch("plant_watering.OutputDevice"),
        ):
            result = plant.setup(_DEFAULT_SENSORS)  # _DEFAULT_SENSORS only has channel 0

        assert result is False

    def test_returns_false_when_hardware_raises(self) -> None:
        plant = PlantControl(
            name="Broken", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        with patch("plant_watering.MCP3008", side_effect=RuntimeError("SPI error")):
            result = plant.setup(_DEFAULT_SENSORS)

        assert result is False
        assert plant._sensor is None  # type: ignore[reportPrivateUsage]


# ── PlantControl.moisture ─────────────────────────────────────────────


class TestPlantControlMoisture:
    def test_returns_zero_with_no_sensor(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=None, threshold=40, water_duration=3
        )
        assert plant.moisture == 0.0

    def test_reads_from_sensor_value(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        _setup_plant(plant, _DEFAULT_SENSORS, sensor_value=0.375)
        assert plant.moisture == pytest.approx(50.0)  # type: ignore[arg-type]

    def test_dry_sensor_reading(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        _setup_plant(plant, _DEFAULT_SENSORS, sensor_value=0.55)
        assert plant.moisture == pytest.approx(0.0)  # type: ignore[arg-type]


# ── PlantControl.check_and_water ──────────────────────────────────────


class TestPlantControlCheckAndWater:
    def _make_plant(
        self,
        threshold: float = 40.0,
        water_duration: float = 2.0,
        sensor_value: float = 0.375,
        last_watered: float = 0.0,
    ) -> tuple[PlantControl, MagicMock]:
        """Return (plant, mock_pump) with sensor and pump attached via setup()."""
        plant = PlantControl(
            name="Test",
            sensor_channel=0,
            relay_pin=17,
            threshold=threshold,
            water_duration=water_duration,
        )
        _, mock_pump = _setup_plant(plant, _DEFAULT_SENSORS, sensor_value=sensor_value)
        plant.last_watered = last_watered
        return plant, mock_pump

    def test_noop_when_sensor_is_none(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=None, threshold=40, water_duration=2
        )
        plant.check_and_water()  # no exception, sensor and pump are None

    def test_skips_watering_when_moisture_above_threshold(self) -> None:
        # sensor_value=0.30 → ~71% moisture, threshold=40 → above threshold
        plant, mock_pump = self._make_plant(threshold=40.0, sensor_value=0.30)
        plant.check_and_water()
        mock_pump.on.assert_not_called()

    def test_skips_watering_during_cooldown(self) -> None:
        # Soil is dry (~14%) but we just watered — cooldown prevents re-watering
        plant, mock_pump = self._make_plant(
            threshold=40.0,
            sensor_value=0.50,
            last_watered=time.monotonic(),
        )
        plant.check_and_water()
        mock_pump.on.assert_not_called()

    def test_waters_when_dry_and_no_cooldown(self) -> None:
        plant, mock_pump = self._make_plant(
            threshold=40.0,
            sensor_value=0.50,  # ~14% moisture → below threshold
            last_watered=0.0,
        )
        with patch("plant_watering.time.sleep"):
            plant.check_and_water()

        mock_pump.on.assert_called_once()
        mock_pump.off.assert_called_once()

    def test_updates_last_watered_after_watering(self) -> None:
        plant, _ = self._make_plant(sensor_value=0.50, last_watered=0.0)
        before = time.monotonic()
        with patch("plant_watering.time.sleep"):
            plant.check_and_water()
        assert plant.last_watered >= before

    def test_pump_sleep_duration_matches_config(self) -> None:
        plant, _ = self._make_plant(water_duration=5.0, sensor_value=0.50, last_watered=0.0)
        with patch("plant_watering.time.sleep") as mock_sleep:
            plant.check_and_water()
        mock_sleep.assert_called_once_with(5.0)


# ── PlantControl.close ────────────────────────────────────────────────


class TestPlantControlClose:
    def test_close_with_no_hardware_is_safe(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=None, threshold=40, water_duration=3
        )
        plant.close()

    def test_close_turns_off_and_releases_pump(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        mock_sensor, mock_pump = _setup_plant(plant, _DEFAULT_SENSORS)
        plant.close()
        mock_pump.off.assert_called_once()
        mock_pump.close.assert_called_once()
        mock_sensor.close.assert_called_once()

    def test_close_with_pump_only(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        _, mock_pump = _setup_plant(plant, _DEFAULT_SENSORS)
        plant._sensor = None  # type: ignore[reportPrivateUsage]
        plant.close()
        mock_pump.off.assert_called_once()
        mock_pump.close.assert_called_once()


# ── build_plants ──────────────────────────────────────────────────────


class TestBuildPlants:
    def test_returns_plants_that_setup_successfully(self) -> None:
        cfg: list[Plant] = [
            {"name": "Good Plant", "sensor_channel": 0, "relay_pin": 17, "threshold": 40, "water_duration": 3}
        ]
        with (
            patch("plant_watering.load_plants", return_value=cfg),
            patch("plant_watering.load_sensor_calibration", return_value=_DEFAULT_SENSORS),
            patch.object(PlantControl, "setup", return_value=True),
        ):
            plants = build_plants()

        assert len(plants) == 1
        assert plants[0].name == "Good Plant"

    def test_excludes_plants_that_fail_setup(self) -> None:
        cfg: list[Plant] = [
            {"name": "No Relay", "sensor_channel": 0, "relay_pin": None, "threshold": 40, "water_duration": 3}
        ]
        with (
            patch("plant_watering.load_plants", return_value=cfg),
            patch("plant_watering.load_sensor_calibration", return_value=_DEFAULT_SENSORS),
            patch.object(PlantControl, "setup", return_value=False),
        ):
            plants = build_plants()

        assert plants == []

    def test_partial_setup_success(self) -> None:
        cfg: list[Plant] = [
            {"name": "Good", "sensor_channel": 0, "relay_pin": 17, "threshold": 40, "water_duration": 3},
            {"name": "Bad", "sensor_channel": 1, "relay_pin": None, "threshold": 40, "water_duration": 3},
        ]
        with (
            patch("plant_watering.load_plants", return_value=cfg),
            patch("plant_watering.load_sensor_calibration", return_value=_DEFAULT_SENSORS),
            patch.object(PlantControl, "setup", side_effect=[True, False]),
        ):
            plants = build_plants()

        assert len(plants) == 1
        assert plants[0].name == "Good"


# ── main ──────────────────────────────────────────────────────────────


class TestMain:
    def test_exits_with_code_1_when_no_plants_ready(self) -> None:
        with (
            patch("plant_watering.build_plants", return_value=[]),
            patch("plant_watering.sys.exit", side_effect=SystemExit(1)),
        ):
            with pytest.raises(SystemExit) as exc_info:
                plant_watering.main()
        assert exc_info.value.code == 1
