"""Windows platform-specific customizations: taskbar identity and dark titlebar.

Two zero-dependency ``ctypes`` tweaks the packaged ``.exe`` needs but Qt does
not apply on its own, mirroring ``ui/macos.py``:

* ``SetCurrentProcessExplicitAppUserModelID`` — pins a stable taskbar
  identity so windows group and pin as VieNeuTTS instead of the interpreter
  or PyInstaller bootstrap name.
* ``DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE)`` — paints the
  titlebar to match the app theme. DWM only follows the OS dark mode; when
  the in-app theme picker disagrees with the OS (manual dark on a light
  system), the frame would otherwise stay light.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # 20H1+; 19 on earlier insider builds
DWMWA_USE_IMMERSIVE_DARK_MODE_FALLBACK = 19
DEFAULT_APP_USER_MODEL_ID = "VieNeuTTS.VieNeuTTS.VieNeuTTS"


def _windll() -> Any | None:
    """``ctypes.windll`` exists only on Windows; never touch it off-platform."""
    if sys.platform != "win32":
        return None
    return getattr(ctypes, "windll", None)


def setup_windows_app(app_id: str = DEFAULT_APP_USER_MODEL_ID) -> bool:
    """Pin the process AppUserModelID (taskbar grouping/pinning identity).

    Returns True if applied, False otherwise (or off-Windows).
    """
    windll = _windll()
    if windll is None:
        return False
    try:
        windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
        return True
    except Exception as exc:  # noqa: BLE001 - non-critical platform enhancement
        logger.debug("Failed to set Windows AppUserModelID: %s", exc)
        return False


def apply_immersive_dark_mode(window: Any, dark: bool) -> bool:
    """Repaint one Qt window's titlebar to match ``dark``.

    ``winId()`` forces native-window creation, so only pass shown windows.
    Returns True if the attribute was applied, False otherwise.
    """
    windll = _windll()
    if windll is None or window is None:
        return False
    try:
        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        dwm = windll.dwmapi
        for attribute in (
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            DWMWA_USE_IMMERSIVE_DARK_MODE_FALLBACK,
        ):
            # Wrap per call: HWND is pointer-sized on 64-bit Windows, which a
            # plain int would truncate to 32 bits.
            result = dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attribute),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if result == 0:  # S_OK
                return True
        return False
    except Exception as exc:  # noqa: BLE001 - non-critical platform enhancement
        logger.debug("Failed to apply immersive dark mode: %s", exc)
        return False


def apply_dark_titlebars(dark: bool) -> int:
    """Apply the titlebar mode to every window of this application.

    Returns how many windows accepted the attribute (0 off-Windows).
    """
    from PySide6.QtGui import QGuiApplication

    applied = 0
    for window in QGuiApplication.allWindows() or []:
        if apply_immersive_dark_mode(window, dark):
            applied += 1
    return applied
