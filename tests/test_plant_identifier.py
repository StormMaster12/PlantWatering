"""Tests for plant_identifier.py."""

import base64
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import plant_identifier
from plant_identifier import (
    PlantIdentification,
    capture_and_identify,
    capture_image,
    identify_plant,
    load_image_as_base64,
)


# ── PlantIdentification dataclass ─────────────────────────────────────


class TestPlantIdentification:
    def test_creates_with_required_fields(self) -> None:
        pi = PlantIdentification(
            plant_name="Monstera deliciosa",
            common_name="Swiss cheese plant",
            confidence="high",
            description="Tropical plant with fenestrated leaves.",
            moisture_threshold=40,
            water_duration=3,
            care_notes="Keep in indirect light.",
        )
        assert pi.plant_name == "Monstera deliciosa"
        assert pi.common_name == "Swiss cheese plant"
        assert pi.image_path == ""  # default

    def test_image_path_defaults_to_empty_string(self) -> None:
        pi = PlantIdentification(
            plant_name="X",
            common_name="Y",
            confidence="low",
            description="",
            moisture_threshold=30,
            water_duration=2,
            care_notes="",
        )
        assert pi.image_path == ""

    def test_explicit_image_path(self) -> None:
        pi = PlantIdentification(
            plant_name="Cactus",
            common_name="Barrel Cactus",
            confidence="medium",
            description="Desert succulent.",
            moisture_threshold=15,
            water_duration=1,
            care_notes="Full sun.",
            image_path="/home/pi/images/plant_123.jpg",
        )
        assert pi.image_path == "/home/pi/images/plant_123.jpg"


# ── capture_image ─────────────────────────────────────────────────────


class TestCaptureImage:
    def test_raises_when_picamera2_not_installed(self) -> None:
        with patch.dict("sys.modules", {"picamera2": None}):
            with pytest.raises(RuntimeError, match="picamera2 not installed"):
                capture_image()

    def test_raises_when_camera_fails(self) -> None:
        mock_module = MagicMock()
        mock_module.Picamera2.side_effect = Exception("CSI not connected")
        with patch.dict("sys.modules", {"picamera2": mock_module}):
            with pytest.raises(RuntimeError, match="Camera capture failed"):
                capture_image()

    def test_returns_jpeg_path_on_success(self, tmp_path: Path) -> None:
        mock_cam = MagicMock()
        mock_module = MagicMock()
        mock_module.Picamera2.return_value = mock_cam

        with (
            patch.dict("sys.modules", {"picamera2": mock_module}),
            patch.object(plant_identifier, "IMAGE_DIR", tmp_path),
            patch("plant_identifier.time.sleep"),
        ):
            path = capture_image()

        assert path.endswith(".jpg")
        assert str(tmp_path) in path
        mock_cam.start.assert_called_once()
        mock_cam.stop.assert_called_once()
        mock_cam.close.assert_called_once()

    def test_capture_calls_capture_file(self, tmp_path: Path) -> None:
        mock_cam = MagicMock()
        mock_module = MagicMock()
        mock_module.Picamera2.return_value = mock_cam

        with (
            patch.dict("sys.modules", {"picamera2": mock_module}),
            patch.object(plant_identifier, "IMAGE_DIR", tmp_path),
            patch("plant_identifier.time.sleep"),
        ):
            path = capture_image()

        mock_cam.capture_file.assert_called_once_with(path)


# ── load_image_as_base64 ──────────────────────────────────────────────


class TestLoadImageAsBase64:
    def test_encodes_file_content(self, tmp_path: Path) -> None:
        img = tmp_path / "test.jpg"
        content = b"fake JPEG bytes"
        img.write_bytes(content)
        result = load_image_as_base64(str(img))
        expected = base64.standard_b64encode(content).decode("utf-8")
        assert result == expected

    def test_empty_file_gives_empty_base64(self, tmp_path: Path) -> None:
        img = tmp_path / "empty.jpg"
        img.write_bytes(b"")
        result = load_image_as_base64(str(img))
        assert result == ""


# ── identify_plant ────────────────────────────────────────────────────


FULL_API_RESPONSE = {
    "plant_name": "Monstera deliciosa",
    "common_name": "Swiss cheese plant",
    "confidence": "high",
    "description": "Needs moderate watering.",
    "moisture_threshold": 40,
    "water_duration": 3,
    "care_notes": "Indirect light, high humidity.",
}


def _make_mock_anthropic(text: str):
    """Build a mocked anthropic module whose client returns *text* as its response."""
    mock_text_block = MagicMock()
    mock_text_block.text = text

    mock_message = MagicMock()
    mock_message.content = [mock_text_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message

    mock_anthropic = MagicMock()
    # Make isinstance(mock_text_block, anthropic.types.TextBlock) pass
    mock_anthropic.types.TextBlock = type(mock_text_block)
    mock_anthropic.Anthropic.return_value = mock_client

    return mock_anthropic, mock_text_block, mock_client


class TestIdentifyPlant:
    def test_raises_without_api_key(self, tmp_path: Path) -> None:
        img = tmp_path / "plant.jpg"
        img.write_bytes(b"data")
        with patch.object(plant_identifier, "ANTHROPIC_API_KEY", ""):
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
                identify_plant(str(img))

    def test_returns_plant_identification_on_success(self, tmp_path: Path) -> None:
        img = tmp_path / "plant.jpg"
        img.write_bytes(b"data")

        mock_anthropic, _, _ = _make_mock_anthropic(json.dumps(FULL_API_RESPONSE))

        with (
            patch.object(plant_identifier, "anthropic", mock_anthropic),
            patch.object(plant_identifier, "ANTHROPIC_API_KEY", "test-key"),
        ):
            result = identify_plant(str(img))

        assert result.plant_name == "Monstera deliciosa"
        assert result.common_name == "Swiss cheese plant"
        assert result.confidence == "high"
        assert result.moisture_threshold == 40
        assert result.water_duration == 3
        assert result.image_path == str(img)

    def test_uses_defaults_for_missing_json_fields(self, tmp_path: Path) -> None:
        img = tmp_path / "plant.jpg"
        img.write_bytes(b"data")

        # Response with only required fields
        minimal = {}
        mock_anthropic, _, _ = _make_mock_anthropic(json.dumps(minimal))

        with (
            patch.object(plant_identifier, "anthropic", mock_anthropic),
            patch.object(plant_identifier, "ANTHROPIC_API_KEY", "test-key"),
        ):
            result = identify_plant(str(img))

        assert result.plant_name == "Unknown plant"
        assert result.common_name == "Unknown"
        assert result.confidence == "low"
        assert result.moisture_threshold == 40
        assert result.water_duration == 3

    def test_raises_when_response_is_not_text_block(self, tmp_path: Path) -> None:
        img = tmp_path / "plant.jpg"
        img.write_bytes(b"data")

        mock_content = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [mock_content]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message

        mock_anthropic = MagicMock()
        # Use a different type so isinstance check fails
        mock_anthropic.types.TextBlock = int
        mock_anthropic.Anthropic.return_value = mock_client

        with (
            patch.object(plant_identifier, "anthropic", mock_anthropic),
            patch.object(plant_identifier, "ANTHROPIC_API_KEY", "test-key"),
        ):
            with pytest.raises(RuntimeError, match="Unexpected response type"):
                identify_plant(str(img))

    def test_raises_when_response_is_not_valid_json(self, tmp_path: Path) -> None:
        img = tmp_path / "plant.jpg"
        img.write_bytes(b"data")

        mock_anthropic, _, _ = _make_mock_anthropic("this is not json }{")

        with (
            patch.object(plant_identifier, "anthropic", mock_anthropic),
            patch.object(plant_identifier, "ANTHROPIC_API_KEY", "test-key"),
        ):
            with pytest.raises(RuntimeError, match="Failed to parse"):
                identify_plant(str(img))

    def test_passes_api_key_to_client(self, tmp_path: Path) -> None:
        img = tmp_path / "plant.jpg"
        img.write_bytes(b"data")

        mock_anthropic, _, mock_client_builder = _make_mock_anthropic(
            json.dumps(FULL_API_RESPONSE)
        )

        with (
            patch.object(plant_identifier, "anthropic", mock_anthropic),
            patch.object(plant_identifier, "ANTHROPIC_API_KEY", "my-secret-key"),
        ):
            identify_plant(str(img))

        mock_anthropic.Anthropic.assert_called_once_with(api_key="my-secret-key")


# ── capture_and_identify ──────────────────────────────────────────────


class TestCaptureAndIdentify:
    def test_chains_capture_then_identify(self, tmp_path: Path) -> None:
        fake_path = str(tmp_path / "captured.jpg")
        mock_result = PlantIdentification(
            plant_name="Fern",
            common_name="Boston Fern",
            confidence="high",
            description="Moisture-loving.",
            moisture_threshold=55,
            water_duration=4,
            care_notes="Keep humid.",
            image_path=fake_path,
        )

        with (
            patch("plant_identifier.capture_image", return_value=fake_path) as mock_cap,
            patch(
                "plant_identifier.identify_plant", return_value=mock_result
            ) as mock_id,
        ):
            result = capture_and_identify()

        mock_cap.assert_called_once()
        mock_id.assert_called_once_with(fake_path)
        assert result.plant_name == "Fern"
