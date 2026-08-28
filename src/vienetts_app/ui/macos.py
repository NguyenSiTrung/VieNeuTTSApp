"""macOS platform-specific customizations: process name, menu bar, and dock icon.

On macOS, when running Python GUI applications without a packaged ``.app`` bundle,
macOS defaults the application menu title and dock name to the interpreter binary
(e.g., ``python3.13``). This module uses Cocoa runtime bindings via ``ctypes`` to
safely configure the application display name and dock icon at runtime with zero
external dependencies.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def setup_macos_app(
    app_name: str = "VieNeuTTS",
    icon_path: str | Path | None = None,
) -> bool:
    """Configure macOS process name, application menu title, and dock icon.

    Args:
        app_name: The user-facing application name to display in the menu bar.
        icon_path: Optional path to an image file (PNG/ICNS) for the macOS Dock.

    Returns:
        bool: True if customization succeeded, False otherwise (or on non-macOS).
    """
    if sys.platform != "darwin":
        return False

    try:
        objc_lib = ctypes.util.find_library("objc")
        if not objc_lib:
            return False
        objc = ctypes.cdll.LoadLibrary(objc_lib)

        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def _msg(
            target: Any,
            sel_name: str,
            *args: Any,
            restype: Any = ctypes.c_void_p,
            argtypes: list[Any] | None = None,
        ) -> Any:
            sel = objc.sel_registerName(sel_name.encode("utf-8"))
            func = objc.objc_msgSend
            func.restype = restype
            types = [ctypes.c_void_p, ctypes.c_void_p]
            if argtypes:
                types.extend(argtypes)
            func.argtypes = types
            return func(target, sel, *args)

        NSString = objc.objc_getClass(b"NSString")
        if not NSString:
            return False

        app_name_ns = _msg(
            NSString,
            "stringWithUTF8String:",
            app_name.encode("utf-8"),
            argtypes=[ctypes.c_char_p],
        )

        # 1. Update NSProcessInfo process name
        NSProcessInfo = objc.objc_getClass(b"NSProcessInfo")
        if NSProcessInfo:
            process_info = _msg(NSProcessInfo, "processInfo")
            if process_info and app_name_ns:
                _msg(
                    process_info,
                    "setProcessName:",
                    app_name_ns,
                    restype=None,
                    argtypes=[ctypes.c_void_p],
                )

        # 2. Update NSApplication mainMenu item and submenu title
        NSApplication = objc.objc_getClass(b"NSApplication")
        if NSApplication:
            ns_app = _msg(NSApplication, "sharedApplication")
            if ns_app:
                main_menu = _msg(ns_app, "mainMenu")
                if main_menu:
                    first_item = _msg(
                        main_menu,
                        "itemAtIndex:",
                        0,
                        argtypes=[ctypes.c_long],
                    )
                    if first_item:
                        _msg(
                            first_item,
                            "setTitle:",
                            app_name_ns,
                            restype=None,
                            argtypes=[ctypes.c_void_p],
                        )
                        submenu = _msg(first_item, "submenu")
                        if submenu:
                            _msg(
                                submenu,
                                "setTitle:",
                                app_name_ns,
                                restype=None,
                                argtypes=[ctypes.c_void_p],
                            )

                # 3. Update macOS Dock icon if icon_path exists
                if icon_path:
                    resolved_icon = Path(icon_path).resolve()
                    if resolved_icon.is_file():
                        NSImage = objc.objc_getClass(b"NSImage")
                        if NSImage:
                            img_alloc = _msg(NSImage, "alloc")
                            path_ns = _msg(
                                NSString,
                                "stringWithUTF8String:",
                                str(resolved_icon).encode("utf-8"),
                                argtypes=[ctypes.c_char_p],
                            )
                            ns_img = _msg(
                                img_alloc,
                                "initWithContentsOfFile:",
                                path_ns,
                                argtypes=[ctypes.c_void_p],
                            )
                            if ns_img:
                                _msg(
                                    ns_app,
                                    "setApplicationIconImage:",
                                    ns_img,
                                    restype=None,
                                    argtypes=[ctypes.c_void_p],
                                )
        return True
    except Exception as exc:  # noqa: BLE001 - non-critical platform enhancement
        logger.debug("Failed to set macOS app name/icon: %s", exc)
        return False
