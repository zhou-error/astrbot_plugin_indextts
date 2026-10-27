# SPDX-License-Identifier: MIT
"""Small text helpers used by the command and auto-TTS paths."""

from __future__ import annotations

import re

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKDOWN_RE = re.compile(r"[*_#>`~]+")
_WHITESPACE_RE = re.compile(r"\s+")


def command_argument(message: str, command: str) -> str:
    """Extract the argument from a command message.

    AstrBot accepts both ``/tts hello`` and ``tts hello`` forms.  Only strip a
    prefix when it is followed by whitespace, otherwise a command such as
    ``/tts_voice`` could be mistaken for the ``tts`` command.
    """

    raw = (message or "").strip()
    for prefix in (f"/{command}", command):
        if raw == prefix:
            return ""
        if raw.startswith(prefix):
            rest = raw[len(prefix) :]
            if rest and rest[0].isspace():
                return rest.strip()
    return raw


def text_for_speech(text: str) -> str:
    """Remove Markdown, code blocks and URLs before TTS synthesis."""

    cleaned = _CODE_FENCE_RE.sub(" ", text or "")
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _MARKDOWN_RE.sub(" ", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()
