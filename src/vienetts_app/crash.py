"""Global crash diagnostic reporter and unhandled exception handler.

Prevents silent crashes on Windows (where console=False windowed executables
redirect stdio to devnull) by recording diagnostic crash logs and displaying
an actionable native error dialog on fatal unhandled exceptions.
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime
import logging
import os
import platform
import re
import stat
import sys
import threading
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any

from vienetts_app import __version__
from vienetts_app.core.settings import default_data_dir

logger = logging.getLogger(__name__)

_INSTALLED = False
_ACTIVE_DATA_DIR: Path | None = None
_CRASH_LOG_NAME = "crash.log"
_MAX_LOG_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB max
_WRITE_LOCK = threading.Lock()

_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer tokens (case-insensitive)
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{4,}"), "Bearer [REDACTED]"),
    # Authorization headers/fields (e.g. Authorization: Basic ..., 'Authorization': '...')
    (
        re.compile(
            r"(?i)(['\"]?authorization['\"]?\s*[:=]\s*['\"]?)"
            r"(?:bearer\s+|basic\s+|token\s+)?[^\s'\",;)}\]]+"
        ),
        r"\1[REDACTED]",
    ),
    # Named API keys / tokens (api_key, llm_api_key, asr_api_key, x-api-key, etc.)
    (
        re.compile(
            r"(?i)(['\"]?[a-z0-9_.-]*(?:api[_-]?key|apikey|access[_-]?token|"
            r"secret[_-]?key|auth[_-]?token)['\"]?\s*[:=]\s*)(['\"]?)([^\s'\",;)}\]]+)\2"
        ),
        r"\1\2[REDACTED]\2",
    ),
    # Provider secret key prefixes (OpenAI / Anthropic / OpenRouter / GitHub / Hugging Face)
    (re.compile(r"\bsk-(?:proj-|ant-|or-v1-)?[A-Za-z0-9_-]{12,}"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "[REDACTED]"),
)

_MACOS_ALERT_SCRIPT = (
    "on run {t, m}\n"
    'display alert t message m as critical buttons {"OK"} '
    'default button "OK" giving up after 300\n'
    "end run"
)


def _redact_secrets(text: str) -> str:
    """Scrub bearer tokens and API keys before writing crash logs or dialogs."""
    redacted = text
    for pattern, replacement in _REDACT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _fallback_crash_dir() -> Path:
    """Return a per-user private temp directory for fallback crash logs."""
    import tempfile

    if os.name == "nt":
        base = Path(tempfile.gettempdir()) / "VieNeuTTSApp-crash"
        base.mkdir(parents=True, exist_ok=True)
        return base
    uid = os.getuid() if hasattr(os, "getuid") else 0
    candidate = Path(tempfile.gettempdir()) / f"vienetts-crash-{uid}"
    try:
        os.mkdir(candidate, 0o700)
    except FileExistsError:
        try:
            info = os.lstat(candidate)
        except OSError:
            return Path(tempfile.mkdtemp(prefix=f"vienetts-crash-{uid}-"))
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (hasattr(os, "getuid") and info.st_uid != uid)
        ):
            return Path(tempfile.mkdtemp(prefix=f"vienetts-crash-{uid}-"))
        with contextlib.suppress(OSError):
            os.chmod(candidate, 0o700)
    except OSError:
        return Path(tempfile.mkdtemp(prefix=f"vienetts-crash-{uid}-"))
    return candidate


def _get_crash_log_path(data_dir: Path | None = None) -> Path:
    base = default_data_dir() if data_dir is None else Path(data_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fallback to a private per-user temp dir if data dir cannot be created
        base = _fallback_crash_dir()
    return base / _CRASH_LOG_NAME


def format_crash_report(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
    thread_name: str | None = None,
) -> str:
    """Format an actionable diagnostic crash report."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tb_lines = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

    # Qt version if available
    qt_ver = "unknown"
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        qt_ver = f"PySide6 {pyside_version} (Qt {qVersion()})"
    except Exception:  # noqa: BLE001
        pass

    thread_info = thread_name or threading.current_thread().name

    report = (
        f"================================================================================\n"
        f"VIENEUTTS CRASH REPORT — {now}\n"
        f"================================================================================\n"
        f"App Version:       {__version__}\n"
        f"Platform:          {platform.platform()} ({sys.platform})\n"
        f"Python Version:    {sys.version.split()[0]} ({platform.architecture()[0]})\n"
        f"Qt Runtime:        {qt_ver}\n"
        f"Thread:            {thread_info} (id: {threading.get_ident()})\n"
        f"Exception Type:    {exc_type.__module__}.{exc_type.__qualname__}\n"
        f"Exception Message: {exc_value}\n"
        f"--------------------------------------------------------------------------------\n"
        f"Traceback:\n{tb_lines}\n"
        f"================================================================================\n\n"
    )
    return _redact_secrets(report)


def write_crash_report(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
    data_dir: Path | None = None,
    thread_name: str | None = None,
) -> Path:
    """Write diagnostic crash report to disk; returns the path to the crash log."""
    log_path = _get_crash_log_path(data_dir)
    report = format_crash_report(exc_type, exc_value, exc_traceback, thread_name=thread_name)
    with _WRITE_LOCK:
        try:
            if log_path.is_symlink():
                raise OSError(f"refusing to write crash log through symlink: {log_path}")

            # Rotate if log exceeds max size
            if log_path.is_file() and log_path.stat().st_size > _MAX_LOG_SIZE_BYTES:
                backup = log_path.with_name(f"{_CRASH_LOG_NAME}.old")
                if backup.is_symlink():
                    raise OSError(f"refusing to replace symlink backup: {backup}")
                with contextlib.suppress(OSError):
                    backup.unlink(missing_ok=True)
                log_path.replace(backup)

            if os.name == "nt":
                with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(report)
                    f.flush()
                    os.fsync(f.fileno())
            else:
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(log_path, flags, 0o600)
                try:
                    with contextlib.suppress(OSError):
                        os.fchmod(fd, 0o600)
                except Exception:  # noqa: BLE001
                    os.close(fd)
                    raise
                with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as f:
                    f.write(report)
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not write crash log to %s: %s", log_path, exc)
    return log_path


def _show_macos_alert(title: str, message: str) -> None:
    """Modal ``display alert`` via osascript — visible without any ObjC/Qt setup.

    A Finder-launched .app has stderr attached to nowhere visible, so the
    stderr fallback below is invisible on macOS (e.g. the "already running"
    second-instance exit looked exactly like "the app won't open").
    ``osascript`` puts up a real system alert from outside the app. Untrusted
    title/message strings are passed via ``on run {t, m}`` argv parameters
    rather than interpolated into AppleScript source. It fails fast when there
    is no window-server session (SSH, headless CI) so the caller can fall
    through to stderr.
    """
    import subprocess

    result = subprocess.run(
        ["/usr/bin/osascript", "-e", _MACOS_ALERT_SCRIPT, "--", str(title), str(message)],
        check=False,
        capture_output=True,
        timeout=320,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"osascript alert failed: {detail}")


def _headless_qt() -> bool:
    """True when Qt renders to no screen (offscreen/minimal QPA): nobody can see
    or dismiss a modal native dialog, which would block the process forever."""
    platform = os.environ.get("QT_QPA_PLATFORM", "").split(":", 1)[0].strip().lower()
    return platform in {"offscreen", "minimal"}


def show_native_error_dialog(title: str, message: str) -> None:
    """Display an emergency native OS message box when GUI is unavailable/crashing."""
    if _headless_qt():
        pass  # stderr only: a modal dialog here would hang a headless run
    elif sys.platform == "win32":
        try:
            MB_ICONERROR = 0x00000010
            MB_OK = 0x00000000
            ctypes.windll.user32.MessageBoxW(0, str(message), str(title), MB_ICONERROR | MB_OK)
            return
        except Exception:  # noqa: BLE001
            pass
    elif sys.platform == "darwin":
        try:
            _show_macos_alert(title, message)
            return
        except Exception:  # noqa: BLE001
            pass

    # Headless, off-Windows/macOS, or the native dialog failed: stderr if writable
    try:
        if sys.stderr and hasattr(sys.stderr, "write") and not sys.stderr.closed:
            sys.stderr.write(f"\n[FATAL ERROR] {title}\n{message}\n")
            sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def handle_unhandled_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
    data_dir: Path | None = None,
    thread_name: str | None = None,
    *,
    show_dialog: bool = True,
) -> None:
    """Global handler for unhandled exceptions (sys.excepthook and threading.excepthook)."""
    # A closed pipe (e.g. ``vienetts-app --smoke ... | head``) is a normal
    # shutdown, not a crash: exit quietly with no log and no dialog.
    if issubclass(exc_type, BrokenPipeError):
        return
    # Clean exit on KeyboardInterrupt / SystemExit
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    logger.critical(
        "Unhandled exception in thread %s: %s",
        thread_name or threading.current_thread().name,
        _redact_secrets(str(exc_value)),
        exc_info=(exc_type, exc_value, exc_traceback),
    )

    log_path = write_crash_report(
        exc_type, exc_value, exc_traceback, data_dir=data_dir, thread_name=thread_name
    )

    if not show_dialog:
        return

    # Show actionable user dialog on main thread or fatal crash
    short_msg = _redact_secrets(str(exc_value).strip() or exc_type.__name__)
    dialog_title = "VieNeuTTS — Lỗi không mong muốn (Unexpected Error)"
    dialog_body = (
        f"Ứng dụng gặp lỗi nghiêm trọng và cần đóng:\n\n"
        f"Chi tiết: {short_msg}\n\n"
        f"Nhật ký lỗi (crash log) đã được lưu tại:\n"
        f"{log_path}\n\n"
        f"Vui lòng gửi tệp này cho nhà phát triển để được hỗ trợ."
    )
    show_native_error_dialog(dialog_title, dialog_body)


def install_crash_handler(data_dir: Path | None = None) -> None:
    """Install global sys.excepthook and threading.excepthook crash reporters."""
    global _INSTALLED, _ACTIVE_DATA_DIR  # noqa: PLW0603
    # Per-dir reinstall: every call re-points the install. The hooks read
    # ``_ACTIVE_DATA_DIR`` live, so a later caller (tests, a relocated
    # profile) takes effect without the caller tracking prior installs.
    if data_dir is not None:
        _ACTIVE_DATA_DIR = Path(data_dir)
    _INSTALLED = True

    def _sys_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        handle_unhandled_exception(
            exc_type, exc_value, exc_traceback, data_dir=_ACTIVE_DATA_DIR, thread_name="MainThread"
        )

    def _thread_hook(args: Any) -> None:
        handle_unhandled_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            data_dir=_ACTIVE_DATA_DIR,
            thread_name=getattr(args.thread, "name", None),
        )

    def _unraisable_hook(args: Any) -> None:
        # ``sys.unraisablehook`` (GC/destructor failures): log and record the
        # crash report only — never open a blocking modal dialog from GC.
        if getattr(args, "exc_type", None) is None:
            return
        exc_value = args.exc_value if args.exc_value is not None else args.exc_type()
        with contextlib.suppress(Exception):  # the reporter must never raise
            handle_unhandled_exception(
                args.exc_type,
                exc_value,
                args.exc_traceback,
                data_dir=_ACTIVE_DATA_DIR,
                thread_name=threading.current_thread().name,
                show_dialog=False,
            )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    sys.unraisablehook = _unraisable_hook
    logger.debug("Crash handler installed")
