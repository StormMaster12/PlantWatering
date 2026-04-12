"""
Shared pytest configuration.

Hardware-specific libraries (gpiozero, picamera2) are stubbed out at the
sys.modules level here so test files can safely import plant_watering and
plant_identifier without needing Raspberry Pi hardware.

This file is loaded by pytest before any test module is imported, ensuring
the stubs are in place before module-level code in the source files runs.
"""

import pathlib
import sys
from unittest.mock import MagicMock


# ── Stub classes for gpiozero ────────────────────────────────────────
# Using real classes (not raw MagicMocks) so that Optional[MCP3008] and
# Optional[OutputDevice] type annotations in plant_watering.py's dataclass
# remain valid at class-definition time.

class _MockMCP3008:
    """Stand-in for gpiozero.MCP3008 on non-Pi systems."""

    def __init__(self, channel: int = 0, **kwargs: object) -> None:
        self.channel = channel
        self.value: float = 0.5  # default mid-range reading

    def close(self) -> None:
        pass


class _MockOutputDevice:
    """Stand-in for gpiozero.OutputDevice on non-Pi systems."""

    def __init__(
        self,
        pin: object = None,
        active_high: bool = True,
        initial_value: bool = False,
        **kwargs: object,
    ) -> None:
        self.pin = pin
        self._on = initial_value

    def on(self) -> None:
        self._on = True

    def off(self) -> None:
        self._on = False

    def close(self) -> None:
        pass


_gpiozero_stub = MagicMock()
_gpiozero_stub.MCP3008 = _MockMCP3008
_gpiozero_stub.OutputDevice = _MockOutputDevice
sys.modules.setdefault("gpiozero", _gpiozero_stub)

# picamera2 is imported lazily inside capture_image(); stub it anyway so
# that any top-level introspection by pytest doesn't crash.
sys.modules.setdefault("picamera2", MagicMock())


# ── Neutralise Pi-specific mkdir calls ──────────────────────────────
# plant_identifier.py runs IMAGE_DIR.mkdir(parents=True, exist_ok=True)
# at import time for /home/pi/… — skip that on dev machines.

_real_mkdir = pathlib.Path.mkdir


def _safe_mkdir(self: pathlib.Path, *args: object, **kwargs: object) -> None:
    if str(self).replace("\\", "/").startswith("/home/pi"):
        return
    _real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]


pathlib.Path.mkdir = _safe_mkdir  # type: ignore[method-assign]
