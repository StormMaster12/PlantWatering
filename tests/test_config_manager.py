"""Tests for config_manager.py."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config_manager
from config_manager import (
    ConfigError,
    add_plant,
    load_plants,
    reload_watering_service,
    remove_plant,
    save_plants,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def one_plant_config(tmp_path: Path):
    """Config file pre-loaded with one plant on channel 0."""
    cfg = tmp_path / "plants_config.json"
    data = {
        "plants": [
            {
                "name": "Rose",
                "sensor_channel": 0,
                "relay_pin": None,
                "threshold": 40,
                "water_duration": 3,
            }
        ]
    }
    cfg.write_text(json.dumps(data))
    with patch.object(config_manager, "CONFIG_PATH", cfg):
        yield cfg


@pytest.fixture
def empty_config(tmp_path: Path):
    """Config file with an empty plants list."""
    cfg = tmp_path / "plants_config.json"
    cfg.write_text(json.dumps({"plants": []}))
    with patch.object(config_manager, "CONFIG_PATH", cfg):
        yield cfg


# ── load_plants ───────────────────────────────────────────────────────


class TestLoadPlants:
    def test_returns_plants_from_valid_file(self, one_plant_config: Path) -> None:
        plants = load_plants()
        assert len(plants) == 1
        assert plants[0]["name"] == "Rose"
        assert plants[0]["sensor_channel"] == 0

    def test_returns_empty_list_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.json"
        with patch.object(config_manager, "CONFIG_PATH", missing):
            plants = load_plants()
        assert plants == []

    def test_returns_empty_list_for_malformed_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json }")
        with patch.object(config_manager, "CONFIG_PATH", bad):
            plants = load_plants()
        assert plants == []

    def test_returns_empty_list_when_plants_key_absent(self, tmp_path: Path) -> None:
        cfg = tmp_path / "no_key.json"
        cfg.write_text(json.dumps({"other": "data"}))
        with patch.object(config_manager, "CONFIG_PATH", cfg):
            plants = load_plants()
        assert plants == []


# ── save_plants ───────────────────────────────────────────────────────


class TestSavePlants:
    def test_writes_plants_to_disk(self, empty_config: Path) -> None:
        plants = [
            {
                "name": "Fern",
                "sensor_channel": 1,
                "relay_pin": 18,
                "threshold": 55,
                "water_duration": 4,
            }
        ]
        save_plants(plants)  # type: ignore[arg-type]
        data = json.loads(empty_config.read_text())
        assert data["plants"][0]["name"] == "Fern"

    def test_overwrites_existing_content(self, one_plant_config: Path) -> None:
        save_plants([])
        data = json.loads(one_plant_config.read_text())
        assert data["plants"] == []

    def test_tmp_file_removed_after_save(self, empty_config: Path) -> None:
        save_plants([])
        tmp_path = empty_config.with_suffix(".tmp")
        assert not tmp_path.exists()


# ── add_plant ─────────────────────────────────────────────────────────


class TestAddPlant:
    def test_adds_valid_plant(self, empty_config: Path) -> None:
        plant = add_plant("Cactus", 3, None, 20, 2)
        assert plant["name"] == "Cactus"
        assert plant["sensor_channel"] == 3
        assert plant["relay_pin"] is None
        assert plant["threshold"] == 20
        assert plant["water_duration"] == 2

    def test_plant_is_persisted(self, empty_config: Path) -> None:
        add_plant("Persisted", 0, None, 40, 3)
        assert len(load_plants()) == 1

    def test_strips_whitespace_from_name(self, empty_config: Path) -> None:
        plant = add_plant("  Basil  ", 1, None, 45, 3)
        assert plant["name"] == "Basil"

    def test_adds_plant_with_relay_pin(self, empty_config: Path) -> None:
        plant = add_plant("Wired", 2, 17, 40, 3)
        assert plant["relay_pin"] == 17

    def test_raises_when_max_plants_reached(self, tmp_path: Path) -> None:
        full = [
            {
                "name": f"Plant{i}",
                "sensor_channel": i,
                "relay_pin": None,
                "threshold": 40,
                "water_duration": 3,
            }
            for i in range(config_manager.MAX_PLANTS)
        ]
        cfg = tmp_path / "full.json"
        cfg.write_text(json.dumps({"plants": full}))
        with patch.object(config_manager, "CONFIG_PATH", cfg):
            with pytest.raises(ConfigError, match="Maximum"):
                add_plant("Extra", 8, None, 40, 3)

    def test_raises_for_channel_below_zero(self, empty_config: Path) -> None:
        with pytest.raises(ConfigError, match="sensor_channel"):
            add_plant("Plant", -1, None, 40, 3)

    def test_raises_for_channel_above_max(self, empty_config: Path) -> None:
        with pytest.raises(ConfigError, match="sensor_channel"):
            add_plant("Plant", 8, None, 40, 3)

    def test_raises_for_duplicate_channel(self, one_plant_config: Path) -> None:
        with pytest.raises(ConfigError, match="already in use"):
            add_plant("Duplicate", 0, None, 40, 3)

    def test_raises_for_threshold_below_zero(self, empty_config: Path) -> None:
        with pytest.raises(ConfigError, match="threshold"):
            add_plant("Plant", 1, None, -1, 3)

    def test_raises_for_threshold_above_100(self, empty_config: Path) -> None:
        with pytest.raises(ConfigError, match="threshold"):
            add_plant("Plant", 1, None, 101, 3)

    def test_raises_for_water_duration_zero(self, empty_config: Path) -> None:
        with pytest.raises(ConfigError, match="water_duration"):
            add_plant("Plant", 1, None, 40, 0)

    def test_raises_for_water_duration_above_30(self, empty_config: Path) -> None:
        with pytest.raises(ConfigError, match="water_duration"):
            add_plant("Plant", 1, None, 40, 31)

    def test_boundary_channel_7(self, empty_config: Path) -> None:
        plant = add_plant("BoundaryHigh", 7, None, 40, 3)
        assert plant["sensor_channel"] == 7

    def test_boundary_threshold_zero(self, empty_config: Path) -> None:
        plant = add_plant("DryLover", 0, None, 0, 1)
        assert plant["threshold"] == 0

    def test_boundary_threshold_100(self, empty_config: Path) -> None:
        plant = add_plant("WetLover", 0, None, 100, 30)
        assert plant["threshold"] == 100

    def test_boundary_water_duration_1(self, empty_config: Path) -> None:
        plant = add_plant("QuickDrip", 0, None, 40, 1)
        assert plant["water_duration"] == 1

    def test_boundary_water_duration_30(self, empty_config: Path) -> None:
        plant = add_plant("LongDrip", 0, None, 40, 30)
        assert plant["water_duration"] == 30


# ── remove_plant ──────────────────────────────────────────────────────


class TestRemovePlant:
    def test_removes_existing_plant(self, one_plant_config: Path) -> None:
        result = remove_plant(0)
        assert result is True
        assert load_plants() == []

    def test_returns_false_when_channel_not_found(self, one_plant_config: Path) -> None:
        result = remove_plant(5)
        assert result is False
        assert len(load_plants()) == 1  # original plant untouched

    def test_preserves_other_plants(self, tmp_path: Path) -> None:
        data = {
            "plants": [
                {
                    "name": "Rose",
                    "sensor_channel": 0,
                    "relay_pin": None,
                    "threshold": 40,
                    "water_duration": 3,
                },
                {
                    "name": "Fern",
                    "sensor_channel": 1,
                    "relay_pin": None,
                    "threshold": 55,
                    "water_duration": 4,
                },
            ]
        }
        cfg = tmp_path / "two.json"
        cfg.write_text(json.dumps(data))
        with patch.object(config_manager, "CONFIG_PATH", cfg):
            result = remove_plant(0)
            assert result is True
            remaining = load_plants()
        assert len(remaining) == 1
        assert remaining[0]["name"] == "Fern"


# ── reload_watering_service ───────────────────────────────────────────


class TestReloadWateringService:
    def test_returns_true_and_success_message_on_ok(self) -> None:
        with patch("config_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            ok, msg = reload_watering_service()
        assert ok is True
        assert "successfully" in msg.lower()

    def test_calls_correct_systemctl_command(self) -> None:
        with patch("config_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            reload_watering_service()
        mock_run.assert_called_once_with(
            ["sudo", "systemctl", "restart", "plant-watering.service"],
            check=True,
            capture_output=True,
            timeout=15,
        )

    def test_returns_false_on_called_process_error(self) -> None:
        with patch("config_manager.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "systemctl", stderr=b"unit not found"
            )
            ok, msg = reload_watering_service()
        assert ok is False
        assert "Failed" in msg

    def test_returns_false_on_unexpected_exception(self) -> None:
        with patch("config_manager.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("permission denied")
            ok, msg = reload_watering_service()
        assert ok is False
        assert "Unexpected" in msg
