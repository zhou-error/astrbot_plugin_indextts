# SPDX-License-Identifier: MIT
"""Audio download, validation and storage helpers.

The security-sensitive rules live here so they can be unit-tested without
importing AstrBot:

* only public HTTP(S) URLs are allowed;
* redirects are rejected rather than followed;
* downloads are limited in size and validated by file signature;
* all writes are atomic (temporary file + ``os.replace``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shutil
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 10 * 1024 * 1024
ALLOWED_AUDIO_SUFFIXES = frozenset(
    {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".amr", ".silk"},
)
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/ogg",
        "application/x-flac",
        "application/x-ogg",
        "binary/octet-stream",
    },
)


class AudioError(RuntimeError):
    """Raised when a reference audio file/URL is not acceptable."""


def _sanitize_url(url: str) -> str:
    """Return a URL suitable for logs: no credentials, query or fragment."""

    try:
        parts = urlsplit(url)
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname or '?'}{port}{parts.path}"
    except (TypeError, ValueError):
        return "<invalid-url>"


def is_public_ip(value: str) -> bool:
    """Return True only for globally routable IP addresses."""

    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def ensure_public_http_url(url: str) -> None:
    """Reject non-HTTP(S), credentialed or private-network URLs."""

    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise AudioError("音频地址格式无效") from exc

    if parts.scheme not in {"http", "https"}:
        raise AudioError("只支持 http/https 音频地址")
    if not parts.hostname:
        raise AudioError("音频地址缺少主机名")
    if parts.username or parts.password:
        raise AudioError("音频地址不能包含用户名或密码")

    port = port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise AudioError("无法解析音频地址") from exc

    addresses = {info[4][0] for info in infos if info[4]}
    if not addresses or any(not is_public_ip(address) for address in addresses):
        raise AudioError("音频地址不是公网地址")


def looks_like_audio(path: Path) -> bool:
    """Check common WAV/MP3/OGG/FLAC/M4A/AMR/SILK file signatures."""

    try:
        with Path(path).open("rb") as file:
            header = file.read(16)
    except OSError:
        return False

    if len(header) < 4:
        return False
    if header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE":
        return True
    if header[:3] == b"ID3":
        return True
    if header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return True
    if header[:4] == b"OggS" or header[:4] == b"fLaC":
        return True
    if header[4:8] == b"ftyp":
        return True
    if header[:5] == b"#!AMR" or header[:8] == b"#!SILK_V":
        return True
    return False


def _atomic_replace(temp_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, destination)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        # Windows may not honour POSIX modes; the write still succeeded.
        logger.debug("无法设置音频文件权限: %s", destination)


def new_output_path(output_dir: Path) -> Path:
    """Return a new output path inside the plugin data directory."""

    output_dir = Path(output_dir)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return output_dir / f"tts_{uuid.uuid4().hex}.wav"


def is_supported_local_audio(path: Path) -> bool:
    """Validate a local reference file selected by an administrator."""

    try:
        path = Path(path)
        if not path.is_file() or path.suffix.lower() not in ALLOWED_AUDIO_SUFFIXES:
            return False
        size = path.stat().st_size
        return 0 < size <= MAX_AUDIO_BYTES and looks_like_audio(path)
    except OSError:
        return False


def copy_local_audio(
    source: Path,
    destination: Path,
    *,
    max_bytes: int = MAX_AUDIO_BYTES,
) -> Path:
    """Atomically copy a local audio file after size/signature checks."""

    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise AudioError("音频文件不存在")
    try:
        if source.stat().st_size > max_bytes:
            raise AudioError("音频文件超过大小限制")
    except OSError as exc:
        raise AudioError("无法读取音频文件") from exc
    if not looks_like_audio(source):
        raise AudioError("文件不是支持的音频格式")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.part",
    )
    try:
        shutil.copyfile(source, temp_path)
        _atomic_replace(temp_path, destination)
        return destination
    except OSError as exc:
        raise AudioError("保存音频文件失败") from exc
    finally:
        temp_path.unlink(missing_ok=True)


async def download_audio(
    url: str,
    destination: Path,
    *,
    timeout: float = 60.0,
    max_bytes: int = MAX_AUDIO_BYTES,
) -> Path:
    """Download a public audio URL to ``destination`` atomically."""

    destination = Path(destination)
    await ensure_public_http_url(url)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.part",
    )
    total = 0
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "GET",
                url,
                headers={"User-Agent": "astrbot-plugin-indextts/2.0"},
            ) as response:
                if 300 <= response.status_code < 400:
                    raise AudioError("音频地址发生重定向，已拒绝")
                response.raise_for_status()

                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if (
                    content_type
                    and not content_type.startswith("audio/")
                    and content_type not in _ALLOWED_CONTENT_TYPES
                ):
                    raise AudioError("下载内容不是音频")

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise AudioError("音频文件超过大小限制")
                    except ValueError:
                        logger.debug("忽略无效的 Content-Length 响应头")

                with temp_path.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise AudioError("音频文件超过大小限制")
                        file.write(chunk)

        if total == 0:
            raise AudioError("下载的音频为空")
        if not looks_like_audio(temp_path):
            raise AudioError("下载内容不是支持的音频格式")
        _atomic_replace(temp_path, destination)
        return destination
    except AudioError:
        logger.warning("音频下载被拒绝: %s", _sanitize_url(url))
        raise
    except httpx.HTTPError as exc:
        logger.warning("音频下载失败: %s (%s)", _sanitize_url(url), type(exc).__name__)
        raise AudioError("音频下载失败") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def cleanup_old_outputs(output_dir: Path, keep: int = 50) -> int:
    """Delete old ``tts_*.wav`` files, keeping the newest ``keep`` files."""

    try:
        keep = max(0, int(keep))
    except (TypeError, ValueError):
        keep = 50

    files: list[tuple[float, Path]] = []
    for path in Path(output_dir).glob("tts_*.wav"):
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            continue

    files.sort(key=lambda item: item[0], reverse=True)
    removed = 0
    for _, path in files[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("删除旧 TTS 文件失败: %s", path, exc_info=True)
    return removed
