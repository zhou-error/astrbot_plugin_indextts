import os
import json
import asyncio
import uuid
import shutil
import time
from pathlib import Path

import httpx
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Plain
from gradio_client import Client, handle_file


@register(
    "astrbot_plugin_indextts",
    "Moonlip Sapling.",
    "基于本地 IndexTTS 的文本转语音插件，支持音色克隆",
    "1.0.0"
)
class IndexTTSPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        # 数据目录
        plugin_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        self.data_dir = plugin_dir / "data"
        self.voice_dir = self.data_dir / "voices"
        self.output_dir = self.data_dir / "generated"
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 加载配置
        self.config_path = self.data_dir / "config.json"
        self.config = self._load_config()

        # Gradio 客户端
        self.client = None
        self._init_client()

    # ─── 配置管理 ───────────────────────────────────────────

    def _load_config(self) -> dict:
        default = {
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
        if self.config_path.exists():
            try:
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                for k, v in default.items():
                    saved.setdefault(k, v)
                return saved
            except Exception:
                logger.warning("配置文件损坏，使用默认配置")
        self._save_config(default)
        return default

    def _save_config(self, config: dict | None = None):
        if config is None:
            config = self.config
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ─── IndexTTS 客户端 ────────────────────────────────────

    def _init_client(self):
        """初始化 Gradio 客户端，返回是否成功"""
        try:
            url = self.config.get("index_tts_url", "http://127.0.0.1:7860/")
            # 确保 URL 格式正确
            if not url.endswith('/'):
                url += '/'

            logger.info(f"正在连接 IndexTTS 服务: {url}")

            # 先测试 HTTP 连接
            import httpx
            try:
                resp = httpx.get(url, timeout=5.0)
                if resp.status_code != 200:
                    logger.warning(f"服务返回状态码: {resp.status_code}")
            except Exception as e:
                logger.error(f"无法访问服务地址: {e}")
                self.client = None
                return False

            # 创建 Gradio 客户端
            self.client = Client(url)

            # 测试客户端是否真的可用
            try:
                # 尝试获取 API 信息
                if hasattr(self.client, 'view_api'):
                    api_info = self.client.view_api(return_format='dict')
                    endpoints = api_info.get('named_endpoints', {})
                    logger.info(f"IndexTTS API 端点: {list(endpoints.keys())}")

                logger.info(f"✅ IndexTTS 服务已成功连接: {url}")
                return True

            except Exception as e:
                logger.error(f"客户端 API 测试失败: {e}")
                self.client = None
                return False

        except Exception as e:
            logger.error(f"❌ IndexTTS 服务连接失败: {e}")
            self.client = None
            return False

    def _get_client(self):
        """获取客户端，若未连接则抛出明确异常"""
        if self.client is None:
            url = self.config.get("index_tts_url", "http://127.0.0.1:7860/")
            raise RuntimeError(
                f"❌ IndexTTS 服务未连接！\n"
                f"   配置地址: {url}\n"
                f"   请检查：\n"
                f"   1. IndexTTS WebUI 是否正在运行（看到 'Running on local URL' 提示）\n"
                f"   2. 浏览器能否访问 {url}\n"
                f"   3. 防火墙是否阻止了连接\n"
                f"   4. 尝试在插件配置中重新设置地址"
            )

        # 额外检查：尝试 ping 客户端
        try:
            # 简单的可用性测试
            if hasattr(self.client, 'predict'):
                return self.client
        except Exception:
            self.client = None
            raise RuntimeError("IndexTTS 客户端已断开连接，请重启插件或服务")

        return self.client

    def _ensure_client(self):
        """确保客户端已连接，如果未连接则尝试重新连接"""
        if self.client is None:
            logger.info("尝试重新连接 IndexTTS 服务...")
            success = self._init_client()
            if not success:
                raise RuntimeError("无法连接到 IndexTTS 服务")
        return self.client

    # ─── 音频工具 ───────────────────────────────────────────

    async def _download_audio(self, url: str, save_path: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                Path(save_path).write_bytes(resp.content)
            return True
        except Exception as e:
            logger.error(f"下载音频失败: {e}")
            return False

    def _get_user_voice_path(self, user_id: str) -> str:
        return str(self.voice_dir / f"{user_id}.wav")

    def _get_reference_audio(self, user_id: str) -> str | None:
        """获取用户的参考音频路径：优先用户个人音色 → 默认配置 → data/voice.wav"""
        user_voice = self._get_user_voice_path(user_id)
        if os.path.exists(user_voice):
            return user_voice
        default = self.config.get("default_reference_audio", "")
        if default and os.path.exists(default):
            return default
        bundled = str(self.data_dir / "voice.wav")
        if os.path.exists(bundled):
            return bundled
        return None

    # ─── TTS 核心 ───────────────────────────────────────────

    async def _generate_tts(self, text: str, reference_audio: str) -> str:
        """调用 IndexTTS 生成语音，返回输出文件路径"""
        if not text or not text.strip():
            raise ValueError("文本不能为空")

        # 确保客户端已连接
        self._ensure_client()
        client = self.client  # 直接使用，不用 _get_client

        if client is None:
            raise RuntimeError("IndexTTS 服务未连接，请检查服务是否运行")

        # 验证参考音频是否存在
        if not os.path.exists(reference_audio):
            raise FileNotFoundError(f"参考音频不存在: {reference_audio}")

        output_file = str(self.output_dir / f"tts_{uuid.uuid4().hex[:8]}.wav")

        cfg = self.config

        result = await asyncio.to_thread(
            client.predict,
            prompt=handle_file(reference_audio),
            text=text,
            infer_mode=cfg.get("infer_mode", "批次推理"),
            max_text_tokens_per_sentence=cfg.get("max_text_tokens_per_sentence", 120),
            sentences_bucket_max_size=cfg.get("sentences_bucket_max_size", 4),
            param_5=cfg.get("do_sample", True),
            param_6=cfg.get("top_p", 0.8),
            param_7=cfg.get("top_k", 30),
            param_8=cfg.get("temperature", 1.0),
            param_9=cfg.get("length_penalty", 0.0),
            param_10=cfg.get("num_beams", 3),
            param_11=cfg.get("repetition_penalty", 10.0),
            param_12=cfg.get("max_mel_tokens", 600),
            api_name="/gen_single",
        )

        # 处理返回结果（可能是 dict 或 str 路径）
        temp_file = None
        if isinstance(result, dict):
            temp_file = result.get("value")
        elif isinstance(result, str):
            temp_file = result

        if not temp_file or not os.path.exists(temp_file):
            raise RuntimeError(f"IndexTTS 返回的音频文件无效: {temp_file}")

        shutil.copy2(temp_file, output_file)
        logger.info(f"TTS 生成成功: {output_file}")
        return output_file

    # ─── 指令: /tts ─────────────────────────────────────────

    @filter.command("tts")
    async def tts(self, event: AstrMessageEvent):
        """文本转语音（支持音色克隆）"""
        text = event.message_str.strip()
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            text = parts[1] if len(parts) > 1 else ""

        if not text:
            yield event.plain_result(
                "用法: /tts <文本>\n"
                "示例: /tts 你好世界\n\n"
                "💡 请先使用 /tts_voice <音频URL> 设置音色，否则将使用默认参考音频"
            )
            return

        user_id = event.get_sender_id()

        # 查找参考音频
        ref_audio = self._get_reference_audio(user_id)
        if ref_audio is None:
            yield event.plain_result(
                "❌ 未设置参考音频！\n"
                "请使用 /tts_voice <音频URL或路径> 设置音色\n"
                "或在插件配置中设置 default_reference_audio"
            )
            return

        yield event.plain_result(f"🎤 正在生成语音...\n📝 文本: {text[:50]}{'...' if len(text) > 50 else ''}")

        try:
            output_file = await self._generate_tts(text, ref_audio)
        except FileNotFoundError:
            yield event.plain_result(f"❌ 参考音频文件不存在，请重新设置音色")
            return
        except RuntimeError as e:
            yield event.plain_result(f"❌ TTS 生成失败: {e}")
            return
        except Exception as e:
            logger.error(f"TTS 异常: {e}")
            yield event.plain_result(f"❌ 未知错误: {e}")
            return

        # 发送语音
        try:
            chain = [Comp.Record(file=output_file, url=output_file)]
            yield event.chain_result(chain)
        except Exception as e:
            logger.error(f"发送语音失败: {e}")
            yield event.plain_result(f"❌ 音频发送失败: {e}\n音频已保存至: {output_file}")

        # 清理旧文件（保留最近 50 个）
        self._cleanup_old_outputs(keep=50)

    # ─── 指令: /tts_voice ───────────────────────────────────

    @filter.command("tts_voice")
    async def tts_set_voice(self, event: AstrMessageEvent):
        """设置个人参考音频（音色克隆）"""
        user_id = event.get_sender_id()
        msg = event.message_str.strip()
        if msg.startswith("/"):
            parts = msg.split(maxsplit=1)
            msg = parts[1] if len(parts) > 1 else ""

        voice_path = self._get_user_voice_path(user_id)

        if not msg:
            # 检查是否是回复语音消息
            # 尝试从消息链中获取被引用的音频
            yield event.plain_result(
                "请提供参考音频:\n"
                "/tts_voice <音频URL>\n"
                "/tts_voice <本地文件路径>\n\n"
                "示例: /tts_voice https://example.com/voice.wav\n"
                "重置: /tts_reset_voice"
            )
            return

        # 如果是 URL
        if msg.startswith("http://") or msg.startswith("https://"):
            yield event.plain_result("📥 正在下载参考音频...")
            success = await self._download_audio(msg, voice_path)
            if not success:
                yield event.plain_result("❌ 音频下载失败，请检查 URL 是否有效")
                return
            yield event.plain_result("✅ 音色已设置！现在可以使用 /tts <文本> 生成语音")
            return

        # 如果是本地文件路径
        if os.path.exists(msg):
            try:
                shutil.copy2(msg, voice_path)
                yield event.plain_result("✅ 音色已设置！现在可以使用 /tts <文本> 生成语音")
            except Exception as e:
                yield event.plain_result(f"❌ 复制音频文件失败: {e}")
            return

        yield event.plain_result(f"❌ 无效的音频路径或 URL: {msg}")

    # ─── 指令: /tts_reset_voice ─────────────────────────────

    @filter.command("tts_reset_voice")
    async def tts_reset_voice(self, event: AstrMessageEvent):
        """重置个人音色，恢复使用默认参考音频"""
        user_id = event.get_sender_id()
        voice_path = self._get_user_voice_path(user_id)
        if os.path.exists(voice_path):
            os.remove(voice_path)
            yield event.plain_result("✅ 已重置音色，下次将使用默认参考音频")
        else:
            yield event.plain_result("ℹ️ 你还没有设置个人音色")

    # ─── 指令: /tts_params ──────────────────────────────────

    @filter.command("tts_params")
    async def tts_params(self, event: AstrMessageEvent):
        """查看当前 TTS 配置"""
        user_id = event.get_sender_id()
        ref = self._get_reference_audio(user_id)
        has_user_voice = os.path.exists(self._get_user_voice_path(user_id))

        lines = [
            "📋 IndexTTS 当前配置:",
            f"  服务地址: {self.config['index_tts_url']}",
            f"  服务状态: {'✅ 已连接' if self.client else '❌ 未连接'}",
            f"  自动 TTS: {'✅ 已开启' if self.config.get('auto_tts_enabled', True) else '❌ 已关闭'}",
            f"  个人音色: {'✅ 已设置' if has_user_voice else '❌ 未设置（使用默认）'}",
            f"  参考音频: {ref or '(无)'}",
            f"  推理模式: {self.config.get('infer_mode', 'N/A')}",
            f"  temperature: {self.config.get('temperature', 1.0)}",
            f"  top_p: {self.config.get('top_p', 0.8)}",
            f"  top_k: {self.config.get('top_k', 30)}",
        ]
        yield event.plain_result("\n".join(lines))

    # ─── 自动捕获语音消息（可选） ────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_voice_message(self, event: AstrMessageEvent):
        """当用户发送语音消息时，自动保存为参考音频（需启用 auto_capture_voice）"""
        if not self.config.get("auto_capture_voice", False):
            return

        try:
            message_chain = event.message_obj.message
        except Exception:
            return

        user_id = event.get_sender_id()

        for comp in message_chain:
            if isinstance(comp, Comp.Record):
                url = getattr(comp, "url", None) or getattr(comp, "file", None)
                if url:
                    voice_path = self._get_user_voice_path(user_id)
                    success = await self._download_audio(str(url), voice_path)
                    if success:
                        yield event.plain_result("🎙️ 已自动捕获语音作为你的参考音色")
                    else:
                        logger.warning(f"自动捕获语音失败: user={user_id}")
                break

    # ─── 清理 ───────────────────────────────────────────────

    def _cleanup_old_outputs(self, keep: int = 50):
        try:
            files = sorted(
                self.output_dir.glob("tts_*.wav"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for f in files[keep:]:
                f.unlink(missing_ok=True)
        except Exception:
            pass

    # ─── 自动 TTS（LLM 响应 → 语音） ─────────────────────────

    def _extract_plain_text(self, chain: list) -> str:
        """从消息链中提取所有纯文本"""
        parts = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
        return "".join(parts)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """拦截 LLM 回复，自动转换为语音"""
        if not self.config.get("auto_tts_enabled", True):
            return

        result = event.get_result()
        chain = result.chain
        text = self._extract_plain_text(chain)

        if not text or not text.strip():
            return

        max_len = self.config.get("max_auto_tts_length", 500)
        if len(text) > max_len:
            logger.info(f"自动 TTS 跳过（文本过长 {len(text)} > {max_len}）")
            return

        user_id = event.get_sender_id()
        ref_audio = self._get_reference_audio(user_id)
        if ref_audio is None:
            logger.warning("自动 TTS 跳过：未找到参考音频")
            return

        logger.info(f"自动 TTS 生成中... user={user_id}, len={len(text)}")
        try:
            output_file = await self._generate_tts(text, ref_audio)
        except Exception as e:
            logger.error(f"自动 TTS 生成失败: {e}")
            return

        chain.append(Comp.Record(file=output_file, url=output_file))
        logger.info(f"自动 TTS 已追加到消息链: {output_file}")

    # ─── 指令: /tts_auto ─────────────────────────────────────

    @filter.command("tts_auto")
    async def tts_auto_toggle(self, event: AstrMessageEvent):
        """开关自动 TTS 模式"""
        msg = event.message_str.strip()
        if msg.startswith("/"):
            parts = msg.split(maxsplit=1)
            msg = parts[1] if len(parts) > 1 else ""

        current = self.config.get("auto_tts_enabled", True)

        if msg.lower() in ("on", "开", "启用", "开启", "1", "true"):
            self.config["auto_tts_enabled"] = True
            self._save_config()
            yield event.plain_result("✅ 自动 TTS 已开启\nLLM 回复将自动转换为语音消息")
        elif msg.lower() in ("off", "关", "禁用", "关闭", "0", "false"):
            self.config["auto_tts_enabled"] = False
            self._save_config()
            yield event.plain_result("🔇 自动 TTS 已关闭\n使用 /tts <文本> 仍可手动生成语音")
        else:
            status = "✅ 已开启" if current else "❌ 已关闭"
            yield event.plain_result(
                f"自动 TTS 状态: {status}\n\n"
                f"用法: /tts_auto on  → 开启自动 TTS\n"
                f"       /tts_auto off → 关闭自动 TTS"
            )

    async def terminate(self):
        """插件卸载时调用"""
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        logger.info("IndexTTS 插件已卸载")
