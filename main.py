# SPDX-License-Identifier: MIT
"""AstrBot IndexTTS plugin: text-to-speech with voice cloning."""

from __future__ import annotations

import asyncio
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .core.audio import (
    AudioError,
    cleanup_old_outputs,
    copy_local_audio,
    download_audio,
    is_supported_local_audio,
)
from .core.client import IndexTTSClient, TTSError, TTSServiceUnavailable
from .core.config import (
    DEFAULT_CONFIG,
    as_bool,
    as_float,
    as_int,
    migrate_legacy_config,
)
from .core.paths import resolve_reference_path, voice_path
from .core.text import command_argument, text_for_speech

MAX_MANUAL_TTS_CHARS = 1000
KEEP_GENERATED_FILES = 50
PLUGIN_NAME = "astrbot_plugin_indextts"


class IndexTTSPlugin(Star):
    """IndexTTS text-to-speech plugin."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.config = config if config is not None else dict(DEFAULT_CONFIG)

        # Persistent data belongs in data/plugin_data, not the plugin folder:
        # the plugin folder is replaced on update/uninstall.
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.voice_dir = self.data_dir / "voices"
        self.output_dir = self.data_dir / "generated"
        self.voice_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        legacy_config = Path(__file__).resolve().parent / "data" / "config.json"
        try:
            migrate_legacy_config(self.config, legacy_config)
        except Exception:
            logger.exception("迁移旧版 data/config.json 失败")

        self.tts_client = IndexTTSClient(
            url=str(self.config.get("index_tts_url", DEFAULT_CONFIG["index_tts_url"])),
            output_dir=self.output_dir,
            params=self._client_params(),
        )
        self._max_manual_chars = MAX_MANUAL_TTS_CHARS

    # ─── 配置 ───────────────────────────────────────────────

    def _client_params(self) -> dict[str, object]:
        return {
            "infer_mode": str(
                self.config.get("infer_mode", DEFAULT_CONFIG["infer_mode"]),
            ),
            "max_text_tokens_per_sentence": as_int(
                self.config.get("max_text_tokens_per_sentence"),
                int(DEFAULT_CONFIG["max_text_tokens_per_sentence"]),
            ),
            "sentences_bucket_max_size": as_int(
                self.config.get("sentences_bucket_max_size"),
                int(DEFAULT_CONFIG["sentences_bucket_max_size"]),
            ),
            "param_5": as_bool(
                self.config.get("do_sample"),
                bool(DEFAULT_CONFIG["do_sample"]),
            ),
            "param_6": as_float(
                self.config.get("top_p"),
                float(DEFAULT_CONFIG["top_p"]),
            ),
            "param_7": as_int(
                self.config.get("top_k"),
                int(DEFAULT_CONFIG["top_k"]),
            ),
            "param_8": as_float(
                self.config.get("temperature"),
                float(DEFAULT_CONFIG["temperature"]),
            ),
            "param_9": as_float(
                self.config.get("length_penalty"),
                float(DEFAULT_CONFIG["length_penalty"]),
            ),
            "param_10": as_int(
                self.config.get("num_beams"),
                int(DEFAULT_CONFIG["num_beams"]),
            ),
            "param_11": as_float(
                self.config.get("repetition_penalty"),
                float(DEFAULT_CONFIG["repetition_penalty"]),
            ),
            "param_12": as_int(
                self.config.get("max_mel_tokens"),
                int(DEFAULT_CONFIG["max_mel_tokens"]),
            ),
        }

    # ─── 路径与状态 ─────────────────────────────────────────

    def _event_voice_path(self, event: AstrMessageEvent) -> Path:
        return voice_path(
            self.voice_dir,
            event.get_platform_id(),
            event.get_sender_id(),
            event.get_session_id(),
        )

    @staticmethod
    def _usable_audio(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _get_reference_audio(self, event: AstrMessageEvent) -> Path | None:
        user_voice = self._event_voice_path(event)
        if self._usable_audio(user_voice):
            return user_voice

        configured = self.config.get("default_reference_audio", "")
        default_audio = resolve_reference_path(self.data_dir, str(configured))
        if self._usable_audio(default_audio):
            return default_audio
        return None

    @staticmethod
    def _auto_key(event: AstrMessageEvent) -> str:
        return f"auto_tts:{event.unified_msg_origin}"

    def _framework_tts_active(self, event: AstrMessageEvent) -> bool:
        """Avoid adding a second voice when AstrBot's TTS provider is active."""

        try:
            config = self.context.get_config()
            settings = config.get("provider_tts_settings", {})
            if not as_bool(settings.get("enable", False), False):
                return False
            getter = getattr(self.context, "get_using_tts_provider", None)
            if not callable(getter):
                return False
            return getter(event.unified_msg_origin) is not None
        except Exception:
            return False

    async def _auto_tts_enabled(self, event: AstrMessageEvent) -> bool:
        default = as_bool(
            self.config.get("auto_tts_enabled", DEFAULT_CONFIG["auto_tts_enabled"]),
            bool(DEFAULT_CONFIG["auto_tts_enabled"]),
        )
        try:
            value = await self.get_kv_data(self._auto_key(event), default)
        except Exception:
            logger.warning("读取会话自动 TTS 设置失败，使用全局默认值")
            return default
        return as_bool(value, default)

    @staticmethod
    def _message_components(event: AstrMessageEvent) -> list[object]:
        getter = getattr(event, "get_messages", None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:
                return []
        message_obj = getattr(event, "message_obj", None)
        return list(getattr(message_obj, "message", []) or [])

    @staticmethod
    def _extract_plain_text(chain: list[object]) -> str:
        parts = []
        for component in chain:
            if isinstance(component, Plain):
                text = str(getattr(component, "text", "") or "").strip()
                if text:
                    parts.append(text)
        return " ".join(parts)

    # ─── 指令: /tts ─────────────────────────────────────────

    @filter.command("tts")
    async def tts(self, event: AstrMessageEvent):
        """文本转语音（支持音色克隆）。"""

        text = command_argument(event.get_message_str(), "tts")
        if not text:
            yield event.plain_result(
                "用法: /tts <文本>\n"
                "示例: /tts 你好世界\n\n"
                "💡 请先使用 /tts_voice 设置音色，或在插件配置中设置默认参考音频",
            )
            return

        if len(text) > self._max_manual_chars:
            yield event.plain_result(
                f"❌ 文本过长，最多 {self._max_manual_chars} 字",
            )
            return

        reference = self._get_reference_audio(event)
        if reference is None:
            yield event.plain_result(
                "❌ 未设置参考音频！\n"
                "请使用 /tts_voice <音频URL> 设置音色，或发送一条语音消息\n"
                "也可以在插件配置中设置 default_reference_audio",
            )
            return

        yield event.plain_result(
            f"🎤 正在生成语音...\n📝 文本: {text[:50]}{'...' if len(text) > 50 else ''}",
        )

        try:
            output_path = await self.tts_client.generate(text, reference)
        except TTSServiceUnavailable:
            logger.warning("IndexTTS 服务不可用")
            yield event.plain_result("❌ IndexTTS 服务不可用，请检查服务地址和运行状态")
            return
        except TTSError as exc:
            logger.warning("IndexTTS 生成失败: %s", type(exc).__name__)
            yield event.plain_result("❌ TTS 生成失败，请稍后重试或查看日志")
            return
        except Exception:
            logger.exception("IndexTTS 未预期异常")
            yield event.plain_result("❌ TTS 生成失败，请查看日志")
            return

        try:
            yield event.chain_result(
                [Comp.Record(file=str(output_path), url=str(output_path))],
            )
        finally:
            await asyncio.to_thread(
                cleanup_old_outputs,
                self.output_dir,
                KEEP_GENERATED_FILES,
            )

    # ─── 指令: /tts_voice ───────────────────────────────────

    @filter.command("tts_voice")
    async def tts_set_voice(self, event: AstrMessageEvent):
        """设置个人参考音频（音色克隆）。"""

        argument = command_argument(event.get_message_str(), "tts_voice")
        destination = self._event_voice_path(event)
        records = [
            component
            for component in self._message_components(event)
            if isinstance(component, Comp.Record)
        ]

        if records:
            try:
                source = await records[0].convert_to_file_path()
                await asyncio.to_thread(
                    copy_local_audio,
                    source,
                    destination,
                )
            except Exception:
                logger.warning("保存语音消息失败")
                yield event.plain_result("❌ 无法读取这条语音，请重新发送或改用音频 URL")
                return
            yield event.plain_result("✅ 音色已设置！现在可以使用 /tts <文本> 生成语音")
            return

        if not argument:
            yield event.plain_result(
                "请提供参考音频:\n"
                "/tts_voice <音频URL>\n"
                "或直接发送一条语音消息并附带 /tts_voice\n\n"
                "示例: /tts_voice https://example.com/voice.wav\n"
                "重置: /tts_reset_voice",
            )
            return

        if argument.lower().startswith(("http://", "https://")):
            yield event.plain_result("📥 正在下载参考音频...")
            try:
                await download_audio(argument, destination)
            except AudioError:
                logger.warning("参考音频下载失败")
                yield event.plain_result("❌ 音频下载失败，请检查 URL 是否为可访问的公网音频")
                return
            yield event.plain_result("✅ 音色已设置！现在可以使用 /tts <文本> 生成语音")
            return

        # Local paths are intentionally admin-only: allowing every user to
        # probe arbitrary files would create a file-existence oracle.
        if not event.is_admin():
            yield event.plain_result(
                "❌ 普通用户不能使用服务器本地路径。\n"
                "请发送语音消息，或使用公网音频 URL。",
            )
            return

        source = Path(argument).expanduser()
        if not is_supported_local_audio(source):
            yield event.plain_result("❌ 音频文件不存在、格式不支持或超过大小限制")
            return
        try:
            await asyncio.to_thread(copy_local_audio, source, destination)
        except AudioError:
            logger.warning("复制本地参考音频失败")
            yield event.plain_result("❌ 设置音色失败，请查看日志")
            return
        yield event.plain_result("✅ 音色已设置！现在可以使用 /tts <文本> 生成语音")

    # ─── 指令: /tts_reset_voice ─────────────────────────────

    @filter.command("tts_reset_voice")
    async def tts_reset_voice(self, event: AstrMessageEvent):
        """重置个人音色，恢复使用默认参考音频。"""

        voice_file = self._event_voice_path(event)
        try:
            existed = voice_file.is_file()
            voice_file.unlink(missing_ok=True)
        except OSError:
            logger.exception("删除个人音色失败")
            yield event.plain_result("❌ 重置音色失败，请查看日志")
            return

        if existed:
            yield event.plain_result("✅ 已重置音色，下次将使用默认参考音频")
        else:
            yield event.plain_result("ℹ️ 你还没有设置个人音色")

    # ─── 指令: /tts_params ──────────────────────────────────

    @filter.command("tts_params")
    async def tts_params(self, event: AstrMessageEvent):
        """查看当前 TTS 配置。"""

        reference = self._get_reference_audio(event)
        has_user_voice = self._usable_audio(self._event_voice_path(event))
        auto_enabled = await self._auto_tts_enabled(event)
        if has_user_voice:
            voice_status = "✅ 已设置个人音色"
        elif reference is not None:
            voice_status = "❌ 未设置个人音色（使用默认参考音频）"
        else:
            voice_status = "❌ 未设置音色（也没有默认参考音频）"

        lines = [
            "📋 IndexTTS 当前配置:",
            f"  自动 TTS（本会话）: {'✅ 已开启' if auto_enabled else '❌ 已关闭'}",
            f"  服务状态: {'✅ 已连接' if self.tts_client.is_connected else '❌ 未连接'}",
            f"  音色状态: {voice_status}",
            f"  推理模式: {self.config.get('infer_mode', 'N/A')}",
            f"  temperature: {self.config.get('temperature', 1.0)}",
            f"  top_p: {self.config.get('top_p', 0.8)}",
            f"  top_k: {self.config.get('top_k', 30)}",
        ]

        if event.is_admin():
            lines.insert(
                3,
                f"  服务地址: {self.config.get('index_tts_url', '(未配置)')}",
            )
            lines.append(f"  参考音频: {reference or '(无)'}")
        else:
            lines.append(
                f"  参考音频: {'✅ 已设置' if reference else '❌ 未设置'}",
            )

        yield event.plain_result("\n".join(lines))

    # ─── 自动捕获语音消息 ───────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_voice_message(self, event: AstrMessageEvent):
        """当用户发送语音消息时，自动保存为参考音频（需启用 auto_capture_voice）。"""

        if not as_bool(
            self.config.get("auto_capture_voice", DEFAULT_CONFIG["auto_capture_voice"]),
            bool(DEFAULT_CONFIG["auto_capture_voice"]),
        ):
            return

        records = [
            component
            for component in self._message_components(event)
            if isinstance(component, Comp.Record)
        ]
        if not records:
            return

        try:
            source = await records[0].convert_to_file_path()
            await asyncio.to_thread(
                copy_local_audio,
                source,
                self._event_voice_path(event),
            )
        except Exception:
            logger.warning("自动捕获语音失败")
            return

        yield event.plain_result("🎙️ 已自动捕获语音作为你的参考音色")

    # ─── 自动 TTS（LLM 响应 → 语音） ─────────────────────────

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """拦截 LLM 回复，自动转换为语音。"""

        if not as_bool(
            self.config.get("auto_tts_enabled", DEFAULT_CONFIG["auto_tts_enabled"]),
            bool(DEFAULT_CONFIG["auto_tts_enabled"]),
        ):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # Only decorate LLM results. Command results are GENERAL_RESULT and
        # must never be synthesized (that would voice /tts_params etc.).
        if not result.is_llm_result():
            return

        # Do not attach a second voice if another TTS plugin already did.
        if any(isinstance(component, Comp.Record) for component in result.chain):
            return

        if self._framework_tts_active(event):
            logger.debug("自动 TTS 跳过：AstrBot 内置 TTS provider 已启用")
            return

        if not await self._auto_tts_enabled(event):
            return

        text = text_for_speech(self._extract_plain_text(result.chain))
        if not text:
            return

        max_length = as_int(
            self.config.get("max_auto_tts_length", DEFAULT_CONFIG["max_auto_tts_length"]),
            int(DEFAULT_CONFIG["max_auto_tts_length"]),
        )
        if max_length <= 0 or len(text) > max_length:
            logger.debug(
                "自动 TTS 跳过：文本长度 %d 超过限制 %d",
                len(text),
                max_length,
            )
            return

        reference = self._get_reference_audio(event)
        if reference is None:
            logger.debug("自动 TTS 跳过：未设置参考音频")
            return

        try:
            output_path = await self.tts_client.generate(text, reference)
        except TTSServiceUnavailable:
            logger.debug("自动 TTS 跳过：服务暂不可用")
            return
        except TTSError:
            logger.warning("自动 TTS 生成失败")
            return
        except Exception:
            logger.exception("自动 TTS 未预期异常")
            return

        result.chain.append(
            Comp.Record(file=str(output_path), url=str(output_path)),
        )
        await asyncio.to_thread(
            cleanup_old_outputs,
            self.output_dir,
            KEEP_GENERATED_FILES,
        )

    # ─── 指令: /tts_auto ─────────────────────────────────────

    @filter.command("tts_auto")
    async def tts_auto_toggle(self, event: AstrMessageEvent):
        """开关本会话的自动 TTS 模式。"""

        argument = command_argument(event.get_message_str(), "tts_auto").lower()
        key = self._auto_key(event)
        current = await self._auto_tts_enabled(event)

        if argument in {"on", "开", "启用", "开启", "1", "true"}:
            try:
                await self.put_kv_data(key, True)
            except Exception:
                logger.exception("保存自动 TTS 会话设置失败")
                yield event.plain_result("❌ 无法保存设置，请查看日志")
                return
            yield event.plain_result(
                "✅ 本会话自动 TTS 已开启\nLLM 回复将自动转换为语音（需先设置参考音频）",
            )
        elif argument in {"off", "关", "禁用", "关闭", "0", "false"}:
            try:
                await self.put_kv_data(key, False)
            except Exception:
                logger.exception("保存自动 TTS 会话设置失败")
                yield event.plain_result("❌ 无法保存设置，请查看日志")
                return
            yield event.plain_result(
                "🔇 本会话自动 TTS 已关闭\n使用 /tts <文本> 仍可手动生成语音",
            )
        else:
            status = "✅ 已开启" if current else "❌ 已关闭"
            yield event.plain_result(
                f"本会话自动 TTS 状态: {status}\n\n"
                "用法: /tts_auto on  → 开启\n"
                "       /tts_auto off → 关闭",
            )

    async def terminate(self) -> None:
        """插件卸载时关闭客户端。"""

        try:
            await self.tts_client.close()
        except Exception:
            logger.exception("关闭 IndexTTS 客户端失败")
        logger.info("IndexTTS 插件已卸载")
