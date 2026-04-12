"""Tests for plant_watering.py."""

import time
from unittest.mock import MagicMock, call, patch

import pytest

import plant_watering
from plant_watering import PlantControl, build_plants, raw_to_moisture


# ── raw_to_moisture ───────────────────────────────────────────────────


class TestRawToMoisture:
    def test_dry_calibration_point_returns_zero(self) -> None:
        # SENSOR_DRY = 0.55 → 0%
        assert raw_to_moisture(0.55) == pytest.approx(0.0)

    def test_wet_calibration_point_returns_100(self) -> None:
        # SENSOR_WET = 0.20 → 100%
        assert raw_to_moisture(0.20) == pytest.approx(100.0)

    def test_midpoint_returns_50(self) -> None:
        # midpoint between 0.55 and 0.20 is 0.375 → 50%
        assert raw_to_moisture(0.375) == pytest.approx(50.0)

    def test_clamps_below_zero_for_very_dry_reading(self) -> None:
        # raw > SENSOR_DRY → would give negative → clamp to 0
        assert raw_to_moisture(0.70) == 0.0

    def test_clamps_above_100_for_very_wet_reading(self) -> None:
        # raw < SENSOR_WET → would exceed 100 → clamp to 100
        assert raw_to_moisture(0.05) == 100.0

    def test_quarter_moisture(self) -> None:
        # raw 0.4625 = 0.55 - 0.25*(0.55-0.20) → 25%
        assert raw_to_moisture(0.4625) == pytest.approx(25.0)


# ── PlantControl.setup ────────────────────────────────────────────────


class TestPlantControlSetup:
    def test_skips_and_returns_false_without_relay_pin(self) -> None:
        plant = PlantControl(
            name="No Relay",
            sensor_channel=0,
            relay_pin=None,
            threshold=40,
            water_duration=3,
        )
        result = plant.setup()
        assert result is False
        assert plant._sensor is None
        assert plant._pump is None

    def test_initialises_hardware_and_returns_true(self) -> None:
        with (
            patch("plant_watering.MCP3008") as mock_mcp,
            patch("plant_watering.OutputDevice") as mock_od,
        ):
            plant = PlantControl(
                name="Wired",
                sensor_channel=2,
                relay_pin=17,
                threshold=40,
                water_duration=3,
            )
            result = plant.setup()

        assert result is True
        mock_mcp.assert_called_once_with(channel=2)
        mock_od.assert_called_once_with(17, active_high=False, initial_value=False)
        assert plant._sensor is mock_mcp.return_value
        assert plant._pump is mock_od.return_value

    def test_returns_false_when_hardware_raises(self) -> None:
        with patch("plant_watering.MCP3008", side_effect=RuntimeError("SPI error")):
            plant = PlantControl(
                name="Broken",
                sensor_channel=0,
                relay_pin=17,
                threshold=40,
                water_duration=3,
            )
            result = plant.setup()

        assert result is False
        assert plant._sensor is None


# ── PlantControl.moisture ─────────────────────────────────────────────


class TestPlantControlMoisture:
    def test_returns_zero_with_no_sensor(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=None, threshold=40, water_duration=3
        )
        assert plant.moisture == 0.0

    def test_reads_from_sensor_value(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=None, threshold=40, water_duration=3
        )
        mock_sensor = MagicMock()
        mock_sensor.value = 0.375  # 50% moisture
        plant._sensor = mock_sensor
        assert plant.moisture == pytest.approx(50.0)

    def test_dry_sensor_reading(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=None, threshold=40, water_duration=3
        )
        mock_sensor = MagicMock()
        mock_sensor.value = 0.55  # fully dry
        plant._sensor = mock_sensor
        assert plant.moisture == pytest.approx(0.0)


# ── PlantControl.check_and_water ──────────────────────────────────────


class TestPlantControlCheckAndWater:
    def _make_plant(
        self,
        threshold: float = 40.0,
        water_duration: float = 2.0,
        sensor_value: float = 0.375,
        last_watered: float = 0.0,
    ) -> PlantControl:
        """Return a PlantControl with mock sensor and pump already attached."""
        plant = PlantControl(
            name="Test",
            sensor_channel=0,
            relay_pin=17,
            threshold=threshold,
            water_duration=water_duration,
        )
        plant._sensor = MagicMock()
        plant._sensor.value = sensor_value
        plant._pump = MagicMock()
        plant._last_watered = last_watered
        return plant

    def test_noop_when_sensor_is_none(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=None, threshold=40, water_duration=2
        )
        # No exception, no pump calls (pump is also None)
        plant.check_and_water()

    def test_skips_watering_when_moisture_above_threshold(self) -> None:
        # sensor_value=0.30 → ~71% moisture, threshold=40 → above threshold
        plant = self._make_plant(threshold=40.0, sensor_value=0.30)
        plant.check_and_water()
        plant._pump.on.assert_not_called()

    def test_skips_watering_during_cooldown(self) -> None:
        # Soil is dry (14%) but we just watered — cooldown prevents re-watering
        plant = self._make_plant(
            threshold=40.0,
            sensor_value=0.50,  # ~14% moisture
            last_watered=time.monotonic(),  # watered right now
        )
        plant.check_and_water()
        plant._pump.on.assert_not_called()

    def test_waters_when_dry_and_no_cooldown(self) -> None:
        plant = self._make_plant(
            threshold=40.0,
            sensor_value=0.50,  # ~14% moisture → below threshold
            last_watered=0.0,  # never watered (monotonic is always > MIN_WATER_GAP from 0)
        )
        with patch("plant_watering.time.sleep"):
            plant.check_and_water()

        plant._pump.on.assert_called_once()
        plant._pump.off.assert_called_once()

    def test_updates_last_watered_after_watering(self) -> None:
        plant = self._make_plant(sensor_value=0.50, last_watered=0.0)
        before = time.monotonic()
        with patch("plant_watering.time.sleep"):
            plant.check_and_water()
        assert plant._last_watered >= before

    def test_pump_sleep_duration_matches_config(self) -> None:
        plant = self._make_plant(water_duration=5.0, sensor_value=0.50, last_watered=0.0)
        with patch("plant_watering.time.sleep") as mock_sleep:
            plant.check_and_water()
        mock_sleep.assert_called_once_with(5.0)


# ── PlantControl.close ────────────────────────────────────────────────


class TestPlantControlClose:
    def test_close_with_no_hardware_is_safe(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=None, threshold=40, water_duration=3
        )
        plant.close()  # should not raise

    def test_close_turns_off_and_releases_pump(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        plant._pump = MagicMock()
        plant._sensor = MagicMock()
        plant.close()
        plant._pump.off.assert_called_once()
        plant._pump.close.assert_called_once()
        plant._sensor.close.assert_called_once()

    def test_close_with_pump_only(self) -> None:
        plant = PlantControl(
            name="Test", sensor_channel=0, relay_pin=17, threshold=40, water_duration=3
        )
        plant._pump = MagicMock()
        # _sensor remains None
        plant.close()
        plant._pump.off.assert_called_once()
        plant._pump.close.assert_called_once()


# ── build_plants ──────────────────────────────────────────────────────


class TestBuildPlants:
    def test_returns_plants_that_setup_successfully(self) -> None:
        cfg = [
            {
                "name": "Good Plant",
                "sensor_channel": 0,
                "relay_pin": 17,
                "threshold": 40,
                "water_duration": 3,
            }
        ]
        with (
            patch("plant_watering.load_plants", return_value=cfg),
            patch.object(PlantControl, "setup", return_value=True),
        ):
            plants = build_plants()

        assert len(plants) == 1
        assert plants[0].name == "Good Plant"

    def test_excludes_plants_that_fail_setup(self) -> None:
        cfg = [
            {
                "name": "No Relay",
                "sensor_channel": 0,
                "relay_pin": None,
                "threshold": 40,
                "water_duration": 3,
            }
        ]
        with (
            patch("plant_watering.load_plants", return_value=cfg),
            patch.object(PlantControl, "setup", return_value=False),
        ):
            plants = build_plants()

        assert plants == []

    def test_partial_setup_success(self) -> None:
        cfg = [
            {
                "name": "Good",
                "sensor_channel": 0,
                "relay_pin": 17,
                "threshold": 40,
                "water_duration": 3,
            },
            {
                "name": "Bad",
                "sensor_channel": 1,
                "relay_pin": None,
                "threshold": 40,
                "water_duration": 3,
            },
        ]
        setup_results = [True, False]
        with (
            patch("plant_watering.load_plants", return_value=cfg),
            patch.object(PlantControl, "setup", side_effect=setup_results),
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
