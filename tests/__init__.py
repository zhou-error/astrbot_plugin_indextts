"""Test package for astrbot_plugin_indextts.

When AstrBot is not installed (for example in the lightweight CI job), install a
minimal ``astrbot.api.logger`` stub before importing the core modules.  The
plugin itself always imports the real logger from ``astrbot.api``.
"""

from __future__ import annotations

import importlib.util
import sys
import types


class _StubLogger:
    """Minimal logger used only when AstrBot is unavailable."""

    def debug(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


if importlib.util.find_spec("astrbot") is None:
    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = _StubLogger()
    astrbot.api = astrbot_api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = astrbot_api
