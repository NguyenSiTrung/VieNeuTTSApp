"""Build-stamped version: release.yml writes this file from the git tag.

Source checkouts have no stamp — ``get_version`` falls back to the
committed ``__version__``. Generated file: never commit it.
"""

from __future__ import annotations

BUILD_VERSION = ""


def get_version(package_fallback: str) -> str:
    """Stamped version when the build wrote one, else the package fallback."""
    stamped = BUILD_VERSION.strip()
    return stamped if stamped else package_fallback
