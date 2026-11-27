"""Cross-platform path and filename normalization utilities (Windows/macOS/Linux).

Handles QML QUrl strings (e.g. ``file:///C:/...``, ``file:///home/...``),
percent-encoded characters, surrounding quotes, Windows extended-length paths
(``\\\\?\\`` and ``\\\\?\\UNC\\``), and Windows reserved device names (CON, PRN, AUX, etc.).
"""

from __future__ import annotations

import ntpath
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

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
        # Authority-bearing form (file://host/share): anything before the
        # first slash is a host, not a path component — "localhost" (like an
        # empty authority) names this machine. A drive URL (file://C:/...) is
        # a plain path, not an authority.
        if not raw.startswith("/") and not re.match(r"^[A-Za-z]:[/\\]", raw):
            authority, sep, rest = raw.partition("/")
            if authority.lower() == "localhost":
                raw = f"/{rest}" if sep else "/"
            elif authority:
                # POSIX network-path form; the UNC conversion below only
                # applies on Windows (a \\host\share literal is mangled on
                # macOS/Linux).
                raw = f"//{authority}/{rest}" if sep else f"//{authority}"
        # Decode percent-encoding (spaces, non-ASCII chars)
        raw = unquote(raw)
        # Check for UNC path in URL: file:////server/share or file://server/share
        if os.name == "nt" and raw.startswith("//"):
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


def to_extended(path_or_url: str | Path | None) -> Path:
    """Windows extended-length form of ``path`` (``\\\\?\\`` prefix).

    On non-Windows hosts (or for empty paths) the normalized path is returned
    unchanged. Relative paths and ``.``/``..`` segments are normalized via
    ``os.path.abspath`` before prepending ``\\\\?\\`` or ``\\\\?\\UNC\\``
    (Win32 skips path normalization for extended-length spellings). Use before
    filesystem calls that must survive the legacy 260-char ``MAX_PATH`` limit
    on Windows.
    """
    path = normalize_local_path(path_or_url)
    if os.name != "nt" or is_empty_path(path):
        return path
    raw = str(path)
    if raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
        return path
    if os.path.__name__ == "ntpath":
        raw = os.path.abspath(raw)
    elif re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\"):
        raw = ntpath.normpath(raw)
    else:
        raw = os.path.abspath(raw)
        if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\"):
            raw = ntpath.normpath(raw)
    if raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
        return Path(raw)
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    if re.match(r"^[A-Za-z]:\\", raw):
        return Path("\\\\?\\" + raw)
    return Path(raw)


def to_short_path(path_or_url: str | Path | None) -> Path:
    """Windows 8.3 short-path spelling of ``path`` — pure ASCII when it exists.

    Some native libraries take paths as narrow ``char*`` and open them through
    the ANSI codepage (``fopen``-style), which cannot represent characters
    outside that codepage — a model dir under ``C:\\Users\\Nguyễn`` fails to
    open even though Python itself handles it. The 8.3 alias names the same
    file in ASCII, so it is the safe spelling to hand such APIs. Returns the
    normalized path unchanged on non-Windows hosts, for paths that do not
    exist, and when the volume has short names disabled.
    """
    path = normalize_local_path(path_or_url)
    if os.name != "nt" or is_empty_path(path):
        return path
    import ctypes  # kernel32 exists only on Windows

    extended = to_extended(path)
    source = str(extended)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short = kernel32.GetShortPathNameW
    get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short.restype = ctypes.c_uint32

    buffer = ctypes.create_unicode_buffer(1024)
    needed = int(get_short(source, buffer, len(buffer)))
    if needed > len(buffer):
        # Return value is the required buffer size when it did not fit.
        buffer = ctypes.create_unicode_buffer(needed)
        needed = int(get_short(source, buffer, needed))
    if 0 < needed < len(buffer):
        value = buffer.value
        if value.startswith("\\\\?\\"):
            stripped = normalize_local_path(value)
            if len(str(stripped)) < 260:
                return stripped
        return Path(value)
    return path


def path_to_file_url(path_or_url: str | Path | None) -> str:
    """Convert any local file or directory path into a valid file:// URL string.

    Handles:
    - None or empty string -> ""
    - Surrounding quotes stripping
    - Windows backslash paths (C:\\... -> file:///C:/...)
    - Windows forward slash paths (C:/... -> file:///C:/...)
    - Windows extended-length paths (\\\\?\\... and \\\\?\\UNC\\...)
    - UNC network shares (\\\\server\\share -> file://server/share)
    - Unix paths (/home/... -> file:///home/...)
    - Already valid file:// URLs (normalized if needed)
    """
    if path_or_url is None:
        return ""
    raw = str(path_or_url).strip()
    if not raw:
        return ""

    # Strip matching surrounding quotes
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1].strip()
        if not raw:
            return ""

    if raw.startswith("file://"):
        after_scheme = raw[7:]
        # Ensure 3 slashes before Windows drive letter: file:///C:/...
        if re.match(r"^[A-Za-z]:[/\\]", after_scheme):
            normalized_after = after_scheme.replace("\\", "/")
            return f"file:///{quote(normalized_after, safe='/:%')}"
        return raw

    # Handle Windows extended-length paths
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\"):
        raw = raw[4:]

    # Normalize backslashes
    clean = raw.replace("\\", "/")
    # Percent-encode characters that are unsafe or structural in a URL
    # (space, #, ?, %, non-ASCII): without this a path like 'Nhạc #1.wav'
    # produces a file:// URL QML truncates at the '#'. '/' separators and
    # ':' (Windows drive letter) stay literal.
    encoded = quote(clean, safe="/:")

    # UNC network share: //server/share or \\server\share
    if clean.startswith("//"):
        return f"file:{encoded}"

    # Windows drive letter: C:/path or /C:/path
    if re.match(r"^/?[A-Za-z]:/", clean):
        if encoded.startswith("/"):
            encoded = encoded[1:]
        return f"file:///{encoded}"

    # Unix absolute path: /home/...
    if encoded.startswith("/"):
        return f"file://{encoded}"

    return f"file:///{encoded}"


_SHORT_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _truncate_utf8(text: str, max_chars: int, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_chars`` chars and ``max_bytes`` UTF-8 bytes."""
    if max_chars <= 0 or max_bytes <= 0:
        return ""
    text = text[:max_chars]
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _is_windows_reserved(name: str) -> bool:
    """True when the stem before the first dot (minus trailing spaces/dots) is reserved."""
    base_stem = name.split(".", 1)[0].rstrip(" .").upper()
    return base_stem in _WINDOWS_RESERVED_STEMS


def _split_extension(name: str) -> tuple[str, str]:
    """Return ``(stem, suffix)`` if ``name`` ends with a short ASCII extension."""
    stem, dot, ext = name.rpartition(".")
    if dot and stem and _SHORT_EXT_RE.fullmatch(ext):
        return stem, f".{ext}"
    return name, ""


def _bound_filename(cleaned: str, max_len: int, max_bytes: int) -> str:
    """Apply character and UTF-8 byte bounds while preserving a short extension."""
    stem, suffix = _split_extension(cleaned)
    if suffix and len(suffix) < max_len and len(suffix.encode("utf-8")) < max_bytes:
        suffix_bytes = len(suffix.encode("utf-8"))
        trimmed_stem = _truncate_utf8(
            stem,
            max_len - len(suffix),
            max_bytes - suffix_bytes,
        ).rstrip(" .")
        if trimmed_stem:
            return f"{trimmed_stem}{suffix}"
    return _truncate_utf8(cleaned, max_len, max_bytes).rstrip(" .")


def sanitize_filename(
    name: str,
    max_len: int = 80,
    fallback: str = "file",
    *,
    max_bytes: int | None = None,
) -> str:
    """Sanitize a candidate string into a safe, valid cross-platform filename.

    - Replaces forbidden characters (\\ / : * ? " < > | and control chars) with space
    - Collapses multiple whitespaces
    - Trims leading/trailing whitespace and dots (including before the extension dot)
    - Checks against Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
    - Bounds length to ``max_len`` characters and a safe UTF-8 byte ceiling
    """
    cleaned = _FORBIDDEN_CHARS_RE.sub(" ", str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback

    # Strip trailing whitespace/dots before an extension dot (e.g. "CON .txt" -> "CON.txt").
    stem, suffix = _split_extension(cleaned)
    if suffix:
        stripped_stem = stem.rstrip(" .")
        cleaned = f"{stripped_stem}{suffix}" if stripped_stem else suffix.lstrip(".")

    if _is_windows_reserved(cleaned):
        cleaned = f"_{cleaned}"

    effective_max_bytes = (
        max_bytes if max_bytes is not None else (200 if max_len <= 200 else min(max_len, 255))
    )
    cleaned = _bound_filename(cleaned, max_len, effective_max_bytes)
    if not cleaned:
        cleaned = fallback

    # Guard against truncation exposing a Windows reserved stem.
    if _is_windows_reserved(cleaned):
        cleaned = _bound_filename(f"_{cleaned}", max_len, effective_max_bytes)

    return cleaned
