"""
Plant watering — log server
Runs alongside plant_watering.py on the same Pi.

  • Receives log records over TCP (Python logging socket protocol)
  • Stores the last LOG_BUFFER_SIZE records in memory
  • Serves a live web dashboard  →  http://<pi-ip>:5000
  • Emails you on WARNING or ERROR (with per-level cooldown)

Dependencies:
    pip install flask

Email setup (Gmail example):
    Create a Gmail "App Password" at myaccount.google.com/apppasswords
    Then export before running (or put in /etc/environment):

        export EMAIL_FROM="youraddress@gmail.com"
        export EMAIL_TO="youraddress@gmail.com"
        export EMAIL_PASSWORD="your-app-password"
        export EMAIL_HOST="smtp.gmail.com"
        export EMAIL_PORT="587"
"""

import collections
import logging
import logging.handlers
import os
import pickle
import smtplib
import socketserver
import struct
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from typing import TypedDict

from flask import Flask, Response, jsonify, render_template_string, request

from config_manager import (
    ConfigError,
    add_plant,
    load_plants,
    reload_watering_service,
    remove_plant,
)

# ════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ════════════════════════════════════════════════════════════════════

LOG_SERVER_HOST   = "0.0.0.0"
LOG_SERVER_PORT   = logging.handlers.DEFAULT_TCP_LOGGING_PORT  # 9020
DASHBOARD_PORT    = 5000
LOG_BUFFER_SIZE   = 500    # max records kept in memory

# Minimum gap between emails for the same log level
EMAIL_COOLDOWN = {
    logging.WARNING:  15 * 60,   # 15 minutes between warning emails
    logging.ERROR:     5 * 60,   # 5 minutes between error emails
    logging.CRITICAL:      60,   # 1 minute between critical emails
}

class EmailConfig(TypedDict):
    emailFrom: str
    to: str
    password: str
    host: str
    port: int

class LogStructure(TypedDict):
    time:     str
    date:     str
    level:    str
    levelno:  int
    message:  str

# Email credentials — read from environment variables
EMAIL_CFG = EmailConfig(
    emailFrom=os.getenv("EMAIL_FROM",     ""),
    to=       os.getenv("EMAIL_TO",       ""),
    password= os.getenv("EMAIL_PASSWORD", ""),
    host=     os.getenv("EMAIL_HOST",     "smtp.gmail.com"),
    port=     int(os.getenv("EMAIL_PORT", "587")),
)



# ════════════════════════════════════════════════════════════════════
#  SHARED STATE
# ════════════════════════════════════════════════════════════════════

log_buffer: collections.deque[LogStructure] = collections.deque(maxlen=LOG_BUFFER_SIZE)
buffer_lock = threading.Lock()

# tracks when the last email was sent per level
last_email_sent: dict[int, float] = {}
email_lock = threading.Lock()

# ════════════════════════════════════════════════════════════════════
#  EMAIL
# ════════════════════════════════════════════════════════════════════

def email_is_configured() -> bool:
    return all([EMAIL_CFG["emailFrom"], EMAIL_CFG["to"], EMAIL_CFG["password"]])


def maybe_send_email(record: logging.LogRecord) -> None:
    """Send an alert email if the level warrants it and cooldown has passed."""
    if not email_is_configured():
        return
    if record.levelno < logging.WARNING:
        return

    cooldown = EMAIL_COOLDOWN.get(record.levelno, 5 * 60)

    with email_lock:
        last = last_email_sent.get(record.levelno, 0.0)
        if time.monotonic() - last < cooldown:
            return
        last_email_sent[record.levelno] = time.monotonic()

    # Build and send in a daemon thread so we don't block the receiver
    threading.Thread(target=_send_email, args=(record,), daemon=True).start()


def _send_email(record: logging.LogRecord) -> None:
    level_name = record.levelname
    plant_name = record.name
    timestamp  = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")

    msg = EmailMessage()
    msg["Subject"] = f"[Plant Watering] {level_name}: {record.getMessage()[:60]}"
    msg["From"]    = EMAIL_CFG["emailFrom"]
    msg["To"]      = EMAIL_CFG["to"]
    msg.set_content(
        f"Level    : {level_name}\n"
        f"Time     : {timestamp}\n"
        f"Source   : {plant_name}\n"
        f"Message  : {record.getMessage()}\n"
        f"\n"
        f"View dashboard: http://raspberrypi.local:{DASHBOARD_PORT}\n"
    )

    try:
        with smtplib.SMTP(EMAIL_CFG["host"], EMAIL_CFG["port"]) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_CFG["emailFrom"], EMAIL_CFG["password"])
            smtp.send_message(msg)
        print(f"[email] sent {level_name} alert for: {record.getMessage()[:60]}")
    except Exception as exc:
        print(f"[email] failed to send: {exc}")


# ════════════════════════════════════════════════════════════════════
#  TCP LOG RECEIVER  (Python logging socket protocol)
# ════════════════════════════════════════════════════════════════════

class LogRecordHandler(socketserver.StreamRequestHandler):
    """
    Handles one persistent TCP connection from a SocketHandler client.
    Each record is sent as a 4-byte big-endian length prefix + pickled dict.
    """
    def handle(self) -> None:
        while True:
            # read the 4-byte length prefix
            chunk = self.connection.recv(4)
            if len(chunk) < 4:
                break
            length = struct.unpack(">L", chunk)[0]

            # read the record payload
            data = b""
            while len(data) < length:
                chunk = self.connection.recv(length - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) < length:
                break

            obj  = pickle.loads(data)
            record = logging.makeLogRecord(obj)

            entry:LogStructure = {
                "time":     datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "date":     datetime.fromtimestamp(record.created).strftime("%Y-%m-%d"),
                "level":    record.levelname,
                "levelno":  record.levelno,
                "message":  record.getMessage(),
            }

            with buffer_lock:
                log_buffer.appendleft(entry)   # newest first

            maybe_send_email(record)


class LogServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


# ════════════════════════════════════════════════════════════════════
#  FLASK DASHBOARD
# ════════════════════════════════════════════════════════════════════

app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plant Watering — Logs</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: ui-monospace, "Cascadia Code", "Fira Code", monospace;
    font-size: 13px;
    background: #0f1117;
    color: #c9d1d9;
    min-height: 100vh;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 24px;
    background: #161b22;
    border-bottom: 1px solid #30363d;
    position: sticky;
    top: 0;
    z-index: 10;
  }

  header h1 {
    font-size: 15px;
    font-weight: 600;
    color: #e6edf3;
    letter-spacing: 0.02em;
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: #3fb950;
    box-shadow: 0 0 6px #3fb950;
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.4; }
  }

  .controls {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .filter-btn {
    padding: 4px 10px;
    border-radius: 4px;
    border: 1px solid #30363d;
    background: transparent;
    color: #8b949e;
    cursor: pointer;
    font-size: 11px;
    font-family: inherit;
    transition: all 0.15s;
  }

  .filter-btn:hover,
  .filter-btn.active {
    background: #21262d;
    color: #e6edf3;
    border-color: #6e7681;
  }

  .filter-btn.active.lvl-WARNING  { border-color: #d29922; color: #d29922; }
  .filter-btn.active.lvl-ERROR    { border-color: #da3633; color: #da3633; }
  .filter-btn.active.lvl-CRITICAL { border-color: #f78166; color: #f78166; }
  .filter-btn.active.lvl-INFO     { border-color: #388bfd; color: #388bfd; }
  .filter-btn.active.lvl-DEBUG    { border-color: #6e7681; color: #6e7681; }

  #last-update {
    color: #484f58;
    font-size: 11px;
  }

  #stats {
    display: flex;
    gap: 16px;
    padding: 10px 24px;
    background: #161b22;
    border-bottom: 1px solid #30363d;
    font-size: 11px;
  }

  .stat { color: #8b949e; }
  .stat span { font-weight: 600; }
  .stat span.w { color: #d29922; }
  .stat span.e { color: #da3633; }
  .stat span.i { color: #388bfd; }

  #log-container {
    padding: 16px 24px;
  }

  .log-row {
    display: grid;
    grid-template-columns: 72px 88px 80px 1fr;
    gap: 12px;
    padding: 5px 8px;
    border-radius: 4px;
    border-left: 3px solid transparent;
    margin-bottom: 2px;
    transition: background 0.1s;
  }

  .log-row:hover { background: #161b22; }

  .log-row.DEBUG    { border-left-color: #484f58; }
  .log-row.INFO     { border-left-color: #388bfd; }
  .log-row.WARNING  { border-left-color: #d29922; background: #1a1500; }
  .log-row.ERROR    { border-left-color: #da3633; background: #1a0000; }
  .log-row.CRITICAL { border-left-color: #f78166; background: #200000; }

  .col-time    { color: #484f58; }
  .col-date    { color: #30363d; }
  .col-level   { font-weight: 700; font-size: 11px; letter-spacing: 0.05em; }

  .col-level.DEBUG    { color: #6e7681; }
  .col-level.INFO     { color: #388bfd; }
  .col-level.WARNING  { color: #d29922; }
  .col-level.ERROR    { color: #da3633; }
  .col-level.CRITICAL { color: #f78166; }

  .col-msg { color: #c9d1d9; word-break: break-word; }
  .col-msg.WARNING  { color: #e3b341; }
  .col-msg.ERROR    { color: #f85149; }
  .col-msg.CRITICAL { color: #f78166; font-weight: 600; }

  #empty {
    text-align: center;
    color: #484f58;
    padding: 60px;
    font-size: 13px;
  }

  .hidden { display: none !important; }
</style>
</head>
<body>

<header>
  <h1>
    <div class="dot"></div>
    Plant Watering — Log Monitor
  </h1>
  <div class="controls">
    <button class="filter-btn active lvl-DEBUG"    data-level="DEBUG">DEBUG</button>
    <button class="filter-btn active lvl-INFO"     data-level="INFO">INFO</button>
    <button class="filter-btn active lvl-WARNING"  data-level="WARNING">WARNING</button>
    <button class="filter-btn active lvl-ERROR"    data-level="ERROR">ERROR</button>
    <button class="filter-btn active lvl-CRITICAL" data-level="CRITICAL">CRITICAL</button>
    <span id="last-update">—</span>
  </div>
</header>

<div id="stats">
  <div class="stat">Total: <span id="s-total">0</span></div>
  <div class="stat">Info: <span class="i" id="s-info">0</span></div>
  <div class="stat">Warnings: <span class="w" id="s-warn">0</span></div>
  <div class="stat">Errors: <span class="e" id="s-err">0</span></div>
</div>

<div id="log-container">
  <div id="empty">No log records yet — waiting for plant_watering.py to connect.</div>
</div>

<script>
const POLL_MS   = 3000;
const activeLvl = new Set(["DEBUG","INFO","WARNING","ERROR","CRITICAL"]);
let   allLogs   = [];

document.querySelectorAll(".filter-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const lvl = btn.dataset.level;
    if (activeLvl.has(lvl)) { activeLvl.delete(lvl); btn.classList.remove("active"); }
    else                    { activeLvl.add(lvl);    btn.classList.add("active");   }
    render();
  });
});

function render() {
  const container = document.getElementById("log-container");
  const filtered  = allLogs.filter(r => activeLvl.has(r.level));

  if (filtered.length === 0) {
    container.innerHTML = '<div id="empty">No records match the current filters.</div>';
    return;
  }

  container.innerHTML = filtered.map(r => `
    <div class="log-row ${r.level}">
      <span class="col-time">${r.time}</span>
      <span class="col-date">${r.date}</span>
      <span class="col-level ${r.level}">${r.level}</span>
      <span class="col-msg ${r.level}">${escHtml(r.message)}</span>
    </div>
  `).join("");
}

function updateStats() {
  document.getElementById("s-total").textContent = allLogs.length;
  document.getElementById("s-info").textContent  = allLogs.filter(r => r.level === "INFO").length;
  document.getElementById("s-warn").textContent  = allLogs.filter(r => r.level === "WARNING").length;
  document.getElementById("s-err").textContent   = allLogs.filter(r => ["ERROR","CRITICAL"].includes(r.level)).length;
}

function escHtml(str) {
  return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

async function poll() {
  try {
    const res  = await fetch("/api/logs");
    const data = await res.json();
    allLogs    = data.logs;
    updateStats();
    render();
    document.getElementById("last-update").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("last-update").textContent = "connection error";
  }
}

poll();
setInterval(poll, POLL_MS);
</script>
</body>
</html>
"""


@app.route("/")
def dashboard() -> str:
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/logs")
def api_logs() -> Response:
    with buffer_lock:
        return jsonify({"logs": list(log_buffer)})


# ── Plant management routes ─────────────────────────────────────────

@app.route("/api/plants", methods=["GET"])
def api_plants() -> Response:
    return jsonify({"plants": load_plants()})


@app.route("/api/plants/add", methods=["POST"])
def api_plants_add() -> Response | tuple[Response, int]:
    data = request.get_json(force=True)
    try:
        plant = add_plant(
            name=           data["name"],
            sensor_channel= int(data["sensor_channel"]),
            relay_pin=      int(data["relay_pin"]) if data.get("relay_pin") not in (None, "", "null") else None,
            threshold=      int(data["threshold"]),
            water_duration= int(data["water_duration"]),
        )
        _, msg = reload_watering_service()
        return jsonify({"success": True, "plant": plant, "service_message": msg})
    except (ConfigError, KeyError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/plants/remove/<int:channel>", methods=["DELETE"])
def api_plants_remove(channel: int) -> Response | tuple[Response, int]:
    removed = remove_plant(channel)
    if not removed:
        return jsonify({"success": False, "error": f"No plant on channel {channel}"}), 404
    _ok, msg = reload_watering_service()
    return jsonify({"success": True, "service_message": msg})


@app.route("/api/identify", methods=["POST"])
def api_identify() -> Response | tuple[Response, int]:
    """
    Trigger a camera capture and plant identification.
    Accepts optional JSON body: {"image_path": "/path/to/existing.jpg"}
    to test identification without a camera.
    """
    try:
        from plant_identifier import capture_and_identify, identify_plant
        body = request.get_json(force=True, silent=True) or {}
        if "image_path" in body:
            result = identify_plant(body["image_path"])
        else:
            result = capture_and_identify()
        return jsonify({
            "success":          True,
            "plant_name":       result.plant_name,
            "common_name":      result.common_name,
            "confidence":       result.confidence,
            "description":      result.description,
            "moisture_threshold": result.moisture_threshold,
            "water_duration":   result.water_duration,
            "care_notes":       result.care_notes,
            "image_path":       result.image_path,
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not email_is_configured():
        print("[email] WARNING: email env vars not set — alerts disabled")
        print("  Set EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD, EMAIL_HOST, EMAIL_PORT")
    else:
        print(f"[email] alerts → {EMAIL_CFG['to']}")

    # Start TCP log receiver in a background thread
    server = LogServer((LOG_SERVER_HOST, LOG_SERVER_PORT), LogRecordHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[log server] listening on port {LOG_SERVER_PORT}")

    # Start Flask dashboard (blocks)
    print(f"[dashboard] http://0.0.0.0:{DASHBOARD_PORT}")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)
