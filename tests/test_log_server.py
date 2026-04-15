"""Tests for log_server.py — Flask routes, email, and TCP log handler."""

import logging
import pickle
import struct
import time
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import log_server
from log_server import (
    LogRecordHandler,
    _send_email,
    email_is_configured,
    log_buffer,
    maybe_send_email,
)


# ── Helpers ──────────────────────────────────────────────────────────


CONFIGURED_EMAIL: dict[str, Any] = {
    "emailFrom": "from@example.com",
    "to": "to@example.com",
    "password": "s3cr3t",
    "host": "smtp.example.com",
    "port": 587,
}

EMPTY_EMAIL: dict[str, Any] = {
    "emailFrom": "",
    "to": "",
    "password": "",
    "host": "smtp.example.com",
    "port": 587,
}


def _make_log_record(
    msg: str = "test message",
    levelno: int = logging.INFO,
    name: str = "test",
) -> logging.LogRecord:
    return logging.makeLogRecord(
        {
            "name": name,
            "levelno": levelno,
            "levelname": logging.getLevelName(levelno),
            "pathname": "test.py",
            "lineno": 1,
            "msg": msg,
            "args": (),
            "exc_info": None,
            "created": time.time(),
        }
    )


def _pack_record(record: logging.LogRecord) -> bytes:
    """Serialise a LogRecord the same way Python's SocketHandler does."""
    d = dict(record.__dict__)
    d["msg"] = record.getMessage()
    d["args"] = None
    payload = pickle.dumps(d)
    return struct.pack(">L", len(payload)) + payload


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state() -> None:  # type: ignore[return]
    """Clear shared module state before every test."""
    log_buffer.clear()
    log_server.last_email_sent.clear()
    yield
    log_buffer.clear()
    log_server.last_email_sent.clear()


@pytest.fixture
def client():
    log_server.app.testing = True
    with log_server.app.test_client() as c:
        yield c


# ── email_is_configured ───────────────────────────────────────────────


class TestEmailIsConfigured:
    def test_returns_true_when_all_fields_set(self) -> None:
        with patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL):
            assert email_is_configured() is True

    def test_returns_false_when_emailFrom_missing(self) -> None:
        cfg = {**CONFIGURED_EMAIL, "emailFrom": ""}
        with patch.object(log_server, "EMAIL_CFG", cfg):
            assert email_is_configured() is False

    def test_returns_false_when_to_missing(self) -> None:
        cfg = {**CONFIGURED_EMAIL, "to": ""}
        with patch.object(log_server, "EMAIL_CFG", cfg):
            assert email_is_configured() is False

    def test_returns_false_when_password_missing(self) -> None:
        cfg = {**CONFIGURED_EMAIL, "password": ""}
        with patch.object(log_server, "EMAIL_CFG", cfg):
            assert email_is_configured() is False


# ── maybe_send_email ──────────────────────────────────────────────────


class TestMaybeSendEmail:
    def test_returns_early_when_email_not_configured(self) -> None:
        with (
            patch.object(log_server, "EMAIL_CFG", EMPTY_EMAIL),
            patch("log_server.threading.Thread") as mock_thread,
        ):
            maybe_send_email(_make_log_record(levelno=logging.ERROR))
        mock_thread.assert_not_called()

    def test_returns_early_for_level_below_warning(self) -> None:
        with (
            patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL),
            patch("log_server.threading.Thread") as mock_thread,
        ):
            maybe_send_email(_make_log_record(levelno=logging.INFO))
        mock_thread.assert_not_called()

    def test_returns_early_when_cooldown_active(self) -> None:
        log_server.last_email_sent[logging.WARNING] = time.monotonic()
        with (
            patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL),
            patch("log_server.threading.Thread") as mock_thread,
        ):
            maybe_send_email(_make_log_record(levelno=logging.WARNING))
        mock_thread.assert_not_called()

    def test_spawns_daemon_thread_when_all_conditions_met(self) -> None:
        mock_thread = MagicMock()
        with (
            patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL),
            patch("log_server.threading.Thread", return_value=mock_thread) as mock_cls,
        ):
            record = _make_log_record(levelno=logging.ERROR)
            maybe_send_email(record)

        mock_cls.assert_called_once_with(
            target=_send_email, args=(record,), daemon=True
        )
        mock_thread.start.assert_called_once()

    def test_records_send_time_to_prevent_immediate_resend(self) -> None:
        mock_thread = MagicMock()
        with (
            patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL),
            patch("log_server.threading.Thread", return_value=mock_thread),
        ):
            before = time.monotonic()
            maybe_send_email(_make_log_record(levelno=logging.ERROR))
        assert log_server.last_email_sent.get(logging.ERROR, 0) >= before

    def test_warning_level_triggers_email(self) -> None:
        mock_thread = MagicMock()
        with (
            patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL),
            patch("log_server.threading.Thread", return_value=mock_thread),
        ):
            maybe_send_email(_make_log_record(levelno=logging.WARNING))
        mock_thread.start.assert_called_once()

    def test_critical_level_triggers_email(self) -> None:
        mock_thread = MagicMock()
        with (
            patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL),
            patch("log_server.threading.Thread", return_value=mock_thread),
        ):
            maybe_send_email(_make_log_record(levelno=logging.CRITICAL))
        mock_thread.start.assert_called_once()


# ── _send_email ───────────────────────────────────────────────────────


class TestSendEmail:
    def test_sends_message_via_smtp(self) -> None:
        record = _make_log_record(msg="Pump relay stuck", levelno=logging.ERROR)
        with (
            patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL),
            patch("log_server.smtplib.SMTP") as mock_smtp_cls,
        ):
            smtp_ctx = mock_smtp_cls.return_value.__enter__.return_value
            _send_email(record)

        smtp_ctx.send_message.assert_called_once()
        sent_msg = smtp_ctx.send_message.call_args[0][0]
        assert "ERROR" in sent_msg["Subject"]

    def test_does_not_raise_on_smtp_failure(self) -> None:
        record = _make_log_record(levelno=logging.ERROR)
        with (
            patch.object(log_server, "EMAIL_CFG", CONFIGURED_EMAIL),
            patch(
                "log_server.smtplib.SMTP", side_effect=ConnectionRefusedError("no server")
            ),
        ):
            _send_email(record)  # must not raise


# ── LogRecordHandler ──────────────────────────────────────────────────


class TestLogRecordHandler:
    def _make_handler(self, recv_side_effect: list) -> LogRecordHandler:
        handler = LogRecordHandler.__new__(LogRecordHandler)
        mock_conn = MagicMock()
        mock_conn.recv.side_effect = recv_side_effect
        handler.connection = mock_conn
        return handler

    def test_appends_record_to_buffer(self) -> None:
        record = _make_log_record("Hello from sensor")
        raw = _pack_record(record)
        handler = self._make_handler(
            [raw[:4], raw[4:], b""]  # length, payload, EOF
        )
        with patch("log_server.maybe_send_email"):
            handler.handle()
        assert len(log_buffer) == 1
        assert log_buffer[0]["message"] == "Hello from sensor"

    def test_stores_correct_level_in_buffer(self) -> None:
        record = _make_log_record("Moisture low", levelno=logging.WARNING)
        raw = _pack_record(record)
        handler = self._make_handler([raw[:4], raw[4:], b""])
        with patch("log_server.maybe_send_email"):
            handler.handle()
        assert log_buffer[0]["level"] == "WARNING"
        assert log_buffer[0]["levelno"] == logging.WARNING

    def test_stops_on_short_length_prefix(self) -> None:
        handler = self._make_handler([b"\x00\x00"])  # only 2 bytes, not 4
        handler.handle()  # should return without appending
        assert len(log_buffer) == 0

    def test_stops_on_incomplete_payload(self) -> None:
        record = _make_log_record("Truncated")
        raw = _pack_record(record)
        # Give the length prefix but no payload data
        handler = self._make_handler([raw[:4], b""])
        handler.handle()
        assert len(log_buffer) == 0

    def test_calls_maybe_send_email_for_each_record(self) -> None:
        record = _make_log_record(levelno=logging.ERROR)
        raw = _pack_record(record)
        handler = self._make_handler([raw[:4], raw[4:], b""])
        with patch("log_server.maybe_send_email") as mock_email:
            handler.handle()
        mock_email.assert_called_once()

    def test_multiple_records_in_sequence(self) -> None:
        r1 = _make_log_record("first")
        r2 = _make_log_record("second")
        raw1, raw2 = _pack_record(r1), _pack_record(r2)
        handler = self._make_handler(
            [raw1[:4], raw1[4:], raw2[:4], raw2[4:], b""]
        )
        with patch("log_server.maybe_send_email"):
            handler.handle()
        assert len(log_buffer) == 2


# ── Flask: GET / ──────────────────────────────────────────────────────


class TestDashboardRoute:
    def test_returns_200_and_html(self, client) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Plant Watering" in resp.data

    def test_content_type_is_html(self, client) -> None:
        resp = client.get("/")
        assert "text/html" in resp.content_type


# ── Flask: GET /api/logs ──────────────────────────────────────────────


class TestApiLogs:
    def test_returns_empty_logs_list(self, client) -> None:
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["logs"] == []

    def test_returns_buffered_entries(self, client) -> None:
        entry = {
            "time": "12:00:00",
            "date": "2026-01-01",
            "level": "INFO",
            "levelno": logging.INFO,
            "message": "Poll started",
        }
        log_buffer.appendleft(entry)  # type: ignore[arg-type]
        resp = client.get("/api/logs")
        data = resp.get_json()
        assert len(data["logs"]) == 1
        assert data["logs"][0]["message"] == "Poll started"


# ── Flask: GET /api/plants ────────────────────────────────────────────


class TestApiPlants:
    def test_returns_plant_list(self, client) -> None:
        plants = [
            {
                "name": "Rose",
                "sensor_channel": 0,
                "relay_pin": None,
                "threshold": 40,
                "water_duration": 3,
            }
        ]
        with patch("log_server.load_plants", return_value=plants):
            resp = client.get("/api/plants")
        assert resp.status_code == 200
        assert resp.get_json()["plants"][0]["name"] == "Rose"

    def test_returns_empty_list_when_no_plants(self, client) -> None:
        with patch("log_server.load_plants", return_value=[]):
            resp = client.get("/api/plants")
        assert resp.get_json()["plants"] == []


# ── Flask: POST /api/plants/add ───────────────────────────────────────


class TestApiPlantsAdd:
    _PAYLOAD = {
        "name": "Cactus",
        "sensor_channel": "3",
        "relay_pin": None,
        "threshold": "20",
        "water_duration": "2",
    }
    _PLANT = {
        "name": "Cactus",
        "sensor_channel": 3,
        "relay_pin": None,
        "threshold": 20,
        "water_duration": 2,
    }

    def test_returns_success_and_plant_on_valid_request(self, client) -> None:
        with (
            patch("log_server.add_plant", return_value=self._PLANT),
            patch("log_server.reload_watering_service", return_value=(True, "ok")),
        ):
            resp = client.post("/api/plants/add", json=self._PAYLOAD)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["plant"]["name"] == "Cactus"

    def test_returns_400_on_config_error(self, client) -> None:
        from config_manager import ConfigError

        with patch(
            "log_server.add_plant", side_effect=ConfigError("channel in use")
        ):
            resp = client.post("/api/plants/add", json=self._PAYLOAD)
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_returns_400_on_missing_key(self, client) -> None:
        with patch("log_server.add_plant", side_effect=KeyError("name")):
            resp = client.post("/api/plants/add", json={})
        assert resp.status_code == 400

    def test_returns_400_on_value_error(self, client) -> None:
        with patch("log_server.add_plant", side_effect=ValueError("bad int")):
            resp = client.post("/api/plants/add", json=self._PAYLOAD)
        assert resp.status_code == 400

    def test_relay_pin_null_string_becomes_none(self, client) -> None:
        """'null' and '' relay_pin values should be coerced to None."""
        captured: dict[str, Any] = {}

        def _capture(**kwargs: Any):
            captured.update(kwargs)
            return self._PLANT

        payload = {**self._PAYLOAD, "relay_pin": "null"}
        with (
            patch("log_server.add_plant", side_effect=_capture),
            patch("log_server.reload_watering_service", return_value=(True, "ok")),
        ):
            client.post("/api/plants/add", json=payload)

        assert captured.get("relay_pin") is None


# ── Flask: DELETE /api/plants/remove/<channel> ────────────────────────


class TestApiPlantsRemove:
    def test_returns_success_when_plant_removed(self, client) -> None:
        with (
            patch("log_server.remove_plant", return_value=True),
            patch("log_server.reload_watering_service", return_value=(True, "ok")),
        ):
            resp = client.delete("/api/plants/remove/0")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_returns_404_when_plant_not_found(self, client) -> None:
        with patch("log_server.remove_plant", return_value=False):
            resp = client.delete("/api/plants/remove/5")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False


# ── Flask: POST /api/identify ─────────────────────────────────────────


class TestApiIdentify:
    def _mock_result(self):
        from plant_identifier import PlantIdentification

        return PlantIdentification(
            plant_name="Fern",
            common_name="Boston Fern",
            confidence="high",
            description="Moisture-loving.",
            moisture_threshold=55,
            water_duration=4,
            care_notes="Mist regularly.",
            image_path="/tmp/plant_1.jpg",
        )

    def test_identify_with_existing_image_path(self, client) -> None:
        result = self._mock_result()
        with patch("plant_identifier.identify_plant", return_value=result):
            resp = client.post(
                "/api/identify", json={"image_path": "/tmp/plant_1.jpg"}
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["plant_name"] == "Fern"

    def test_identify_without_image_captures_from_camera(self, client) -> None:
        result = self._mock_result()
        with patch("plant_identifier.capture_and_identify", return_value=result):
            resp = client.post("/api/identify", json={})
        assert resp.status_code == 200
        assert resp.get_json()["common_name"] == "Boston Fern"

    def test_returns_500_on_exception(self, client) -> None:
        with patch(
            "plant_identifier.capture_and_identify",
            side_effect=RuntimeError("camera not connected"),
        ):
            resp = client.post("/api/identify", json={})
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False
        assert "camera not connected" in body["error"]
