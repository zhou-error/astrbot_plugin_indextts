# SPDX-License-Identifier: MIT
"""Unit tests for core helpers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from core.audio import (
    AudioError,
    cleanup_old_outputs,
    copy_local_audio,
    download_audio,
    is_public_ip,
    looks_like_audio,
)
from core.config import (
    as_bool,
    as_float,
    as_int,
    merge_legacy_config,
    migrate_legacy_config,
)
from core.paths import resolve_reference_path, voice_path
from core.text import command_argument, text_for_speech

WAV_HEADER = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVEfmt "


class VoicePathTests(unittest.TestCase):
    def test_path_stays_inside_voice_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            voice_dir = Path(temp_dir) / "voices"
            path = voice_path(voice_dir, "aiocqhttp", "../../evil")
            self.assertEqual(path.parent, voice_dir)
            self.assertNotIn("..", path.name)

    def test_platform_and_user_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            voice_dir = Path(temp_dir)
            first = voice_path(voice_dir, "platform-a", "user")
            second = voice_path(voice_dir, "platform-b", "user")
            third = voice_path(voice_dir, "platform-a", "other")
            self.assertNotEqual(first, second)
            self.assertNotEqual(first, third)

    def test_empty_user_falls_back_to_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            voice_dir = Path(temp_dir)
            first = voice_path(voice_dir, "platform", "", "session-a")
            second = voice_path(voice_dir, "platform", "", "session-b")
            self.assertNotEqual(first, second)

    def test_relative_default_reference_uses_data_dir(self) -> None:
        resolved = resolve_reference_path(Path("data"), "voices/ref.wav")
        self.assertEqual(resolved, Path("data") / "voices" / "ref.wav")

    def test_empty_default_reference_is_none(self) -> None:
        self.assertIsNone(resolve_reference_path(Path("data"), "  "))


class TextTests(unittest.TestCase):
    def test_command_argument_handles_slash_and_plain_forms(self) -> None:
        self.assertEqual(command_argument("/tts hello", "tts"), "hello")
        self.assertEqual(command_argument("tts hello", "tts"), "hello")
        self.assertEqual(command_argument("/tts", "tts"), "")
        self.assertEqual(command_argument("tts", "tts"), "")
        self.assertEqual(command_argument("/tts\nhello", "tts"), "hello")

    def test_command_argument_does_not_strip_prefix_of_another_command(self) -> None:
        self.assertEqual(
            command_argument("/tts_voice https://example.com/a.wav", "tts"),
            "/tts_voice https://example.com/a.wav",
        )

    def test_text_for_speech_removes_markdown_urls_and_code(self) -> None:
        cleaned = text_for_speech(
            "**你好** https://example.com ```print('x')``` 世界",
        )
        self.assertEqual(cleaned, "你好 世界")


class ConfigTests(unittest.TestCase):
    def test_as_bool(self) -> None:
        self.assertTrue(as_bool("true"))
        self.assertTrue(as_bool("开"))
        self.assertFalse(as_bool("false"))
        self.assertFalse(as_bool("off"))
        self.assertTrue(as_bool(1))
        self.assertFalse(as_bool("unknown", False))

    def test_as_int_and_float(self) -> None:
        self.assertEqual(as_int("12", 3), 12)
        self.assertEqual(as_int("bad", 3), 3)
        self.assertEqual(as_float("1.5", 0.0), 1.5)
        self.assertEqual(as_float("bad", 0.0), 0.0)

    def test_merge_legacy_only_known_keys(self) -> None:
        target = {"index_tts_url": "old", "unknown": "keep"}
        applied = merge_legacy_config(
            target,
            {"index_tts_url": "new", "unknown": "ignored", "top_k": 5},
        )
        self.assertEqual(target["index_tts_url"], "new")
        self.assertEqual(target["unknown"], "keep")
        self.assertEqual(target["top_k"], 5)
        self.assertEqual(applied, {"index_tts_url": "new", "top_k": 5})

    def test_migrate_legacy_config_renames_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy = Path(temp_dir) / "config.json"
            legacy.write_text(json.dumps({"top_k": 7}), encoding="utf-8")
            target = {"top_k": 30}
            self.assertTrue(migrate_legacy_config(target, legacy))
            self.assertEqual(target["top_k"], 7)
            self.assertFalse(legacy.exists())
            self.assertTrue((Path(temp_dir) / "config.json.migrated").exists())

    def test_invalid_legacy_config_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy = Path(temp_dir) / "config.json"
            legacy.write_text("{not-json", encoding="utf-8")
            target = {"top_k": 30}
            self.assertFalse(migrate_legacy_config(target, legacy))
            self.assertEqual(target["top_k"], 30)
            self.assertTrue(legacy.exists())


class AudioTests(unittest.TestCase):
    def test_public_ip_detection(self) -> None:
        self.assertTrue(is_public_ip("8.8.8.8"))
        self.assertFalse(is_public_ip("127.0.0.1"))
        self.assertFalse(is_public_ip("10.0.0.1"))
        self.assertFalse(is_public_ip("::1"))

    def test_audio_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            good = Path(temp_dir) / "good.wav"
            good.write_bytes(WAV_HEADER + b"\x00" * 32)
            bad = Path(temp_dir) / "bad.txt"
            bad.write_text("not audio", encoding="utf-8")
            self.assertTrue(looks_like_audio(good))
            self.assertFalse(looks_like_audio(bad))

    def test_copy_local_audio_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.wav"
            source.write_bytes(WAV_HEADER + b"\x00" * 32)
            destination = Path(temp_dir) / "out" / "voice.wav"
            result = copy_local_audio(source, destination)
            self.assertEqual(result, destination)
            self.assertTrue(destination.exists())
            self.assertTrue(looks_like_audio(destination))

    def test_copy_local_audio_rejects_non_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.txt"
            source.write_text("not audio", encoding="utf-8")
            destination = Path(temp_dir) / "voice.wav"
            with self.assertRaises(AudioError):
                copy_local_audio(source, destination)
            self.assertFalse(destination.exists())

    def test_download_rejects_private_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "voice.wav"
            with self.assertRaises(AudioError):
                asyncio.run(download_audio("http://127.0.0.1/a.wav", destination))
            self.assertFalse(destination.exists())

    def test_cleanup_keeps_newest_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            paths = []
            for index in range(4):
                path = output_dir / f"tts_{index}.wav"
                path.write_bytes(WAV_HEADER)
                os.utime(path, (100 + index, 100 + index))
                paths.append(path)

            removed = cleanup_old_outputs(output_dir, keep=2)
            self.assertEqual(removed, 2)
            self.assertFalse(paths[0].exists())
            self.assertFalse(paths[1].exists())
            self.assertTrue(paths[2].exists())
            self.assertTrue(paths[3].exists())


if __name__ == "__main__":
    unittest.main()
