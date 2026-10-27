# SPDX-License-Identifier: MIT
"""Path helpers for per-user voice files and configured reference audio."""

from __future__ import annotations

import hashlib
from pathlib import Path


def user_voice_key(platform_id: str, user_id: str, session_id: str = "") -> str:
    """Return a stable, filesystem-safe key for a user's voice.

    Platform identifiers are not always safe file names (they may contain
    slashes, colons, Unicode, or be empty), so the key is a truncated SHA-256
    digest instead of the raw identifier.
    """

    identity = (user_id or "").strip() or f"session:{(session_id or '').strip()}"
    payload = f"{platform_id or 'unknown'}\0{identity}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def voice_path(
    voice_dir: Path,
    platform_id: str,
    user_id: str,
    session_id: str = "",
) -> Path:
    """Return the voice file path for one user/session."""

    return Path(voice_dir) / f"{user_voice_key(platform_id, user_id, session_id)}.wav"


def resolve_reference_path(data_dir: Path, configured: str) -> Path | None:
    """Resolve the configured default reference audio path.

    Relative paths are resolved against the plugin data directory so the
    behaviour does not depend on AstrBot's current working directory.
    """

    value = (configured or "").strip()
    if not value:
        return None

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(data_dir) / path
    return path
