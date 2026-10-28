# SPDX-License-Identifier: MIT
"""Async wrapper around the synchronous ``gradio_client`` IndexTTS API."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger
from gradio_client import Client, handle_file

from .audio import MAX_AUDIO_BYTES, looks_like_audio, new_output_path

DEFAULT_TTS_TIMEOUT = 180.0
DEFAULT_RETRY_COOLDOWN = 30.0


class TTSError(RuntimeError):
    """Raised for expected TTS failures (invalid input, bad output, timeout)."""


class TTSServiceUnavailable(TTSError):
    """Raised when the IndexTTS service cannot be reached."""


class IndexTTSClient:
    """Lazy, async-facing IndexTTS client.

    Networking and ``gradio_client`` work happens in worker threads.  Only one
    synthesis request is allowed at a time by default because a local GPU
    service is rarely able to process concurrent requests efficiently.
    """

    def __init__(
        self,
        url: str,
        output_dir: Path,
        params: dict[str, Any],
        *,
        timeout: float = DEFAULT_TTS_TIMEOUT,
        retry_cooldown: float = DEFAULT_RETRY_COOLDOWN,
    ) -> None:
        self._url = (url or "").strip()
        self._output_dir = Path(output_dir)
        self._params = dict(params)
        self._timeout = timeout
        self._retry_cooldown = retry_cooldown
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(1)
        self._next_retry_at = 0.0
        self._closed = False

    @property
    def is_connected(self) -> bool:
        """Whether a client object currently exists (not a live health check)."""

        return self._client is not None

    async def close(self) -> None:
        """Stop accepting work, wait for an in-flight request, then close."""

        self._closed = True
        async with self._semaphore:
            async with self._lock:
                client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(self._close_sync, client)

    @staticmethod
    def _close_sync(client: Client) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    async def _discard_client(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.to_thread(self._close_sync, client)
            except Exception:
                logger.debug("关闭 IndexTTS 客户端失败", exc_info=True)

    async def _ensure_client(self) -> Client:
        if self._closed:
            raise TTSServiceUnavailable("插件正在停止")
        if self._client is not None:
            return self._client

        if time.monotonic() < self._next_retry_at:
            raise TTSServiceUnavailable("IndexTTS 服务暂不可用，请稍后重试")

        async with self._lock:
            if self._closed:
                raise TTSServiceUnavailable("插件正在停止")
            if self._client is not None:
                return self._client
            try:
                client = await asyncio.to_thread(self._build_client)
            except Exception as exc:
                self._next_retry_at = time.monotonic() + self._retry_cooldown
                raise TTSServiceUnavailable("无法连接到 IndexTTS 服务") from exc
            if self._closed:
                await asyncio.to_thread(self._close_sync, client)
                raise TTSServiceUnavailable("插件正在停止")
            self._client = client
            self._next_retry_at = 0.0
            return self._client

    def _build_client(self) -> Client:
        url = self._url
        if not url:
            raise TTSError("IndexTTS 服务地址未配置")
        if not url.endswith("/"):
            url += "/"

        try:
            with httpx.Client(timeout=5.0) as http:
                response = http.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TTSError("IndexTTS 服务地址无法访问") from exc

        client = Client(url, verbose=False)
        try:
            info = client.view_api(return_format="dict")
            endpoints = info.get("named_endpoints", {}) if isinstance(info, dict) else {}
            if "/gen_single" not in endpoints:
                raise TTSError("IndexTTS 服务缺少 /gen_single 接口")
            return client
        except Exception:
            try:
                self._close_sync(client)
            except Exception:
                logger.debug("关闭初始化失败的客户端失败", exc_info=True)
            raise

    async def generate(self, text: str, reference_audio: Path) -> Path:
        """Synthesize ``text`` with ``reference_audio`` and return a WAV path."""

        if not text or not text.strip():
            raise TTSError("文本不能为空")

        reference_audio = Path(reference_audio)
        if not reference_audio.is_file():
            raise TTSError("参考音频不存在")
        try:
            if reference_audio.stat().st_size > MAX_AUDIO_BYTES:
                raise TTSError("参考音频文件过大")
        except OSError as exc:
            raise TTSError("无法读取参考音频") from exc
        if not looks_like_audio(reference_audio):
            raise TTSError("参考音频格式不支持")

        async with self._semaphore:
            if self._closed:
                raise TTSServiceUnavailable("插件正在停止")
            client = await self._ensure_client()

            output_path = new_output_path(self._output_dir)
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._predict_sync, client, text, reference_audio),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError as exc:
                await self._discard_client()
                raise TTSError("IndexTTS 生成超时") from exc
            except Exception as exc:
                await self._discard_client()
                raise TTSError("IndexTTS 生成失败") from exc

            source = self._result_path(result)
            if source is None or not source.is_file():
                raise TTSError("IndexTTS 未返回有效音频")
            if not looks_like_audio(source):
                raise TTSError("IndexTTS 返回的文件不是有效音频")

            temp_path = output_path.with_name(
                f".{output_path.name}.{uuid.uuid4().hex}.part",
            )
            try:
                await asyncio.to_thread(shutil.copyfile, source, temp_path)
                os.replace(temp_path, output_path)
            except OSError as exc:
                raise TTSError("保存生成的音频失败") from exc
            finally:
                temp_path.unlink(missing_ok=True)
                try:
                    source.unlink(missing_ok=True)
                except OSError:
                    pass

            try:
                os.chmod(output_path, 0o600)
            except OSError:
                pass
            return output_path

    def _predict_sync(
        self,
        client: Client,
        text: str,
        reference_audio: Path,
    ) -> Any:
        return client.predict(
            prompt=handle_file(str(reference_audio)),
            text=text,
            **self._params,
            api_name="/gen_single",
        )

    @staticmethod
    def _result_path(result: Any) -> Path | None:
        if isinstance(result, str | os.PathLike):
            value: Any = result
        elif isinstance(result, dict):
            value = result.get("value")
        elif isinstance(result, tuple | list) and result:
            value = result[0]
        else:
            return None

        if isinstance(value, dict):
            value = value.get("value") or value.get("path") or value.get("name")
        if not value:
            return None
        try:
            return Path(value)
        except TypeError:
            return None
