"""Cross-platform path and filename normalization utilities (Windows/macOS/Linux).

Handles QML QUrl strings (e.g. ``file:///C:/...``, ``file:///home/...``),
percent-encoded characters, surrounding quotes, Windows extended-length paths
(``\\\\?\\`` and ``\\\\?\\UNC\\``), and Windows reserved device names (CON, PRN, AUX, etc.).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

# Windows reserved device names (case-insensitive, with or without any extension).
# In Vietnamese, titles like "Con", "Nul", or "Aux" are common words ("Con" = child/creature)
# but writing them as a bare filename on Windows raises [Errno 22] or PermissionError.
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# Cross-platform forbidden characters in filenames: \ / : * ? " < > | and control characters.
_FORBIDDEN_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
def is_empty_path(path_or_url: str | Path | None) -> bool:
    """Return True if path is None, whitespace, or resolves to empty/current dir ('.')."""
    if path_or_url is None:
        return True
    raw = str(path_or_url).strip()
    return not raw or raw == "."



def normalize_local_path(path_or_url: str | Path | None) -> Path:
    """Normalize any path or QUrl representation into a clean, valid local Path.

    Handles:
    - ``None`` or empty string -> ``Path("")``
    - Leading/trailing whitespace
    - Matching surrounding quotes (e.g. Windows Explorer "Copy as path": ``"C:\\path\\file.txt"``)
    - QUrl file schemes (``file:///C:/path``, ``file:///home/user/path``, ``file:////server/share``)
    - URL percent-encoding (e.g. ``%20`` -> space, UTF-8 percent bytes)
    - Windows leading slash before drive letter (e.g. ``/C:/path`` -> ``C:/path``)
    - Windows extended-length prefixes (``\\\\?\\C:\\...``, ``\\\\?\\UNC\\...``)
    """
    if path_or_url is None:
        return Path("")
    raw = str(path_or_url).strip()
    if not raw:
        return Path("")

    # Strip surrounding single or double quotes (e.g. "C:\Users\..." from Windows Explorer)
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1].strip()
        if not raw:
            return Path("")

    # Handle file:// URLs
    if raw.startswith("file://"):
        # Strip scheme
        raw = raw[7:]
        # Decode percent-encoding (spaces, non-ASCII chars)
        raw = unquote(raw)
        # Check for UNC path in URL: file:////server/share or file://server/share
        if raw.startswith("//"):
            # UNC path (network share)
            raw = "\\\\" + raw[2:].replace("/", "\\")
        elif re.match(r"^/[A-Za-z]:[/\\]", raw):
            # Windows drive letter preceded by stray slash: /C:/path -> C:/path
            raw = raw[1:]
        elif os.name == "nt" and re.match(r"^[A-Za-z]:[/\\]", raw):
            pass  # Already a Windows drive path

    # Handle Windows extended-length paths
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\"):
        raw = raw[4:]

    # Remove stray leading slash before Windows drive letter even if not file:// URL
    if re.match(r"^/[A-Za-z]:[/\\]", raw):
        raw = raw[1:]

    return Path(raw)


def sanitize_filename(name: str, max_len: int = 80, fallback: str = "file") -> str:
    """Sanitize a candidate string into a safe, valid cross-platform filename.

    - Replaces forbidden characters (\\ / : * ? " < > | and control chars) with space
    - Collapses multiple whitespaces
    - Trims leading/trailing whitespace and trailing dots (forbidden on Windows)
    - Checks against Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
    - Bounds length to ``max_len`` characters
    """
    cleaned = _FORBIDDEN_CHARS_RE.sub(" ", str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback

    # Truncate to max_len
    cleaned = cleaned[:max_len].rstrip(" .")
    if not cleaned:
        cleaned = fallback

    # Check for Windows reserved device names
    # e.g., "CON", "con.wav", "aux.txt"
    base_stem = cleaned.split(".")[0].upper()
    if base_stem in _WINDOWS_RESERVED_STEMS:
        cleaned = f"_{cleaned}"

    return cleaned
