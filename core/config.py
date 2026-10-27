# SPDX-License-Identifier: MIT
"""Configuration defaults, coercion helpers and legacy config migration."""

from __future__ import annotations

import json
import logging
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "index_tts_url": "http://127.0.0.1:7860/",
    "default_reference_audio": "",
    "auto_capture_voice": False,
    "auto_tts_enabled": True,
    "max_auto_tts_length": 500,
    "infer_mode": "批次推理",
    "max_text_tokens_per_sentence": 120,
    "sentences_bucket_max_size": 4,
    "do_sample": True,
    "top_p": 0.8,
    "top_k": 30,
    "temperature": 1.0,
    "length_penalty": 0.0,
    "num_beams": 3,
    "repetition_penalty": 10.0,
    "max_mel_tokens": 600,
}

CONFIG_KEYS = tuple(DEFAULT_CONFIG)


def as_bool(value: Any, default: bool = False) -> bool:
    """Coerce common config representations to bool."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "开", "开启", "启用"}:
            return True
        if normalized in {"0", "false", "no", "off", "关", "关闭", "禁用"}:
            return False
    return default


def as_int(value: Any, default: int) -> int:
    """Coerce a value to int, returning ``default`` on failure."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float) -> float:
    """Coerce a value to float, returning ``default`` on failure."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def merge_legacy_config(
    target: MutableMapping[str, Any],
    legacy: dict[str, Any],
) -> dict[str, Any]:
    """Copy keys known by the current plugin from an old config mapping."""

    applied: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        if key in legacy:
            target[key] = legacy[key]
            applied[key] = legacy[key]
    return applied


def migrate_legacy_config(
    config: MutableMapping[str, Any],
    legacy_path: Path,
    logger: logging.Logger | None = None,
) -> bool:
    """Migrate the old ``data/config.json`` into the AstrBot config once.

    The legacy file is renamed to ``config.json.migrated`` after a successful
    save so a temporary save failure does not silently discard the user's
    values on the next restart.
    """

    log = logger or logging.getLogger(__name__)
    legacy_path = Path(legacy_path)
    if not legacy_path.is_file():
        return False

    try:
        payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("旧版配置文件无法读取，跳过迁移: %s", legacy_path)
        return False

    if not isinstance(payload, dict):
        log.warning("旧版配置文件格式不是对象，跳过迁移: %s", legacy_path)
        return False

    applied = merge_legacy_config(config, payload)
    save = getattr(config, "save_config", None)
    if callable(save):
        try:
            save()
        except Exception:
            log.exception("保存迁移后的插件配置失败，旧配置保留在原位")
            return False

    try:
        legacy_path.replace(legacy_path.with_name(f"{legacy_path.name}.migrated"))
    except OSError:
        log.warning("旧版配置文件迁移后重命名失败", exc_info=True)

    log.info("已从旧版 data/config.json 迁移 %d 项配置", len(applied))
    return True
