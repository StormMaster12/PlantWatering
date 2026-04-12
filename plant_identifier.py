"""
plant_identifier.py
Captures an image from the Pi Camera Module and uses the Claude API
to identify the plant and recommend hydration settings.

Dependencies:
    pip install anthropic picamera2

Hardware:
    Raspberry Pi Camera Module connected via CSI ribbon cable.
    Enable the camera with: sudo raspi-config → Interface Options → Camera
"""

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ════════════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Where captured images are saved (kept for the dashboard preview)
IMAGE_DIR = Path("/home/pi/plant-watering/images")
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

# ════════════════════════════════════════════════════════════════════
#  DATA
# ════════════════════════════════════════════════════════════════════

@dataclass
class PlantIdentification:
    plant_name:       str           # e.g. "Monstera deliciosa"
    common_name:      str           # e.g. "Swiss cheese plant"
    confidence:       str           # "high" | "medium" | "low"
    description:      str           # brief care summary
    moisture_threshold: int         # % below which watering triggers (0–100)
    water_duration:   int           # seconds to run pump per cycle
    care_notes:       str           # extra tips
    image_path:       str = ""      # path to the captured image

# ════════════════════════════════════════════════════════════════════
#  CAMERA
# ════════════════════════════════════════════════════════════════════

def capture_image() -> str:
    """
    Capture a still image from the Pi Camera Module.
    Returns the path to the saved JPEG.
    Raises RuntimeError if the camera is unavailable.
    """
    try:
        from picamera2 import Picamera2
    except ImportError as exc:
        raise RuntimeError(
            "picamera2 not installed. Run: pip install picamera2"
        ) from exc

    timestamp  = int(time.time())
    image_path = str(IMAGE_DIR / f"plant_{timestamp}.jpg")

    try:
        cam = Picamera2()
        config = cam.create_still_configuration(
            main={"size": (1920, 1080)},
            lores={"size": (640, 480)},
        )
        cam.configure(config)
        cam.start()
        time.sleep(2)   # allow auto-exposure to settle
        cam.capture_file(image_path)
        cam.stop()
        cam.close()
        log.info("Image captured: %s", image_path)
        return image_path
    except Exception as exc:
        raise RuntimeError(f"Camera capture failed: {exc}") from exc


def load_image_as_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


# ════════════════════════════════════════════════════════════════════
#  CLAUDE API  —  identify plant + recommend hydration
# ════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """
You are a botanist and plant care expert. When given an image of a plant,
identify it and recommend watering settings for an automated drip irrigation
system. The moisture sensor reads 0–100% where:
  0%   = bone dry
  100% = saturated

Return ONLY valid JSON — no markdown, no explanation outside the object.

Schema:
{
  "plant_name":          "Latin name or best guess",
  "common_name":         "Common English name",
  "confidence":          "high | medium | low",
  "description":         "One sentence about this plant's water needs",
  "moisture_threshold":  <integer 0–100, water when moisture drops below this>,
  "water_duration":      <integer seconds to run pump per cycle, typically 2–6>,
  "care_notes":          "Any important extra tips in one sentence"
}

Moisture threshold guidance:
  Drought-tolerant (cacti, succulents, ZZ plant): 15–25
  Average (pothos, spider plant, monstera):        35–45
  Moisture-loving (ferns, peace lily, orchid):     50–60

If you cannot confidently identify the plant from the image, still return
valid JSON with your best guess and confidence "low".
"""


def identify_plant(image_path: str) -> PlantIdentification:
    """
    Send the captured image to Claude and parse the response into
    a PlantIdentification dataclass.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set"
        )

    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    b64data = load_image_as_base64(image_path)

    log.info("Sending image to Claude for identification...")

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type":       "image",
                        "source": {
                            "type":       "base64",
                            "media_type": "image/jpeg",
                            "data":       b64data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Please identify this plant and recommend "
                            "automated watering settings for it."
                        ),
                    },
                ],
            }
        ],
    )

    content_block = message.content[0]
    if not isinstance(content_block, anthropic.types.TextBlock):
        raise RuntimeError(
            f"Unexpected response type from Claude: {type(content_block).__name__}"
        )
    raw = content_block.text.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("Claude returned non-JSON: %s", raw)
        raise RuntimeError(f"Failed to parse Claude response as JSON: {exc}") from exc

    return PlantIdentification(
        plant_name=         data.get("plant_name",        "Unknown plant"),
        common_name=        data.get("common_name",       "Unknown"),
        confidence=         data.get("confidence",        "low"),
        description=        data.get("description",       ""),
        moisture_threshold= int(data.get("moisture_threshold", 40)),
        water_duration=     int(data.get("water_duration",      3)),
        care_notes=         data.get("care_notes",        ""),
        image_path=         image_path,
    )


# ════════════════════════════════════════════════════════════════════
#  COMBINED  —  capture + identify in one call
# ════════════════════════════════════════════════════════════════════

def capture_and_identify() -> PlantIdentification:
    """Capture an image from the camera, then identify the plant."""
    image_path = capture_image()
    return identify_plant(image_path)


# ════════════════════════════════════════════════════════════════════
#  CLI  —  run standalone for testing
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    if len(sys.argv) > 1:
        # Test with an existing image: python plant_identifier.py myplant.jpg
        image_path = sys.argv[1]
        log.info("Using provided image: %s", image_path)
        result = identify_plant(image_path)
    else:
        # Capture from camera
        result = capture_and_identify()

    print("\n── Identification result ──────────────────────────")
    print(f"  Plant       : {result.plant_name} ({result.common_name})")
    print(f"  Confidence  : {result.confidence}")
    print(f"  Description : {result.description}")
    print(f"  Threshold   : {result.moisture_threshold}%")
    print(f"  Pump time   : {result.water_duration}s per cycle")
    print(f"  Care notes  : {result.care_notes}")
    print(f"  Image       : {result.image_path}")
