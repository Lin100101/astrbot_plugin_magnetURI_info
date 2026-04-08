"""
AstrBot 插件：whatslink.info 磁链解析器
- 自动识别消息中的 magnet: 链接
- 调用 https://whatslink.info/api/v1/link?url=... 获取资源信息
- 支持插件配置：timeout（毫秒），useForward（合并转发，QQ/OneBot），showScreenshot（显示截图）

中文注释已添加。
"""
from __future__ import annotations

import base64
import collections
import io
import ipaddress
import os
import random
import re
import tempfile
import time
import urllib.parse
import aiohttp
import asyncio
from typing import List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image, Node, Nodes


MAGNET_RE = re.compile(
    r"(magnet:\?xt=urn:btih:(?:[A-Fa-f0-9]{40}|[A-Za-z2-7]{32})[^\s]*)",
    re.IGNORECASE,
)
API_URL = "https://whatslink.info/api/v1/link"


def _human_readable_size(num: int) -> str:
    """将字节数格式化为人类可读的字符串（中文单位）。"""
    if num is None:
        return "未知"
    try:
        num = int(num)
    except Exception:
        return str(num)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024:
            return f"{num:.2f}{unit}"
        num /= 1024
    return f"{num:.2f}PB"


def _first_callable(obj, names: List[str]):
    for n in names:
        f = getattr(obj, n, None)
        if callable(f):
            return f
    return None


def _log_debug(msg: str):
    try:
        f = getattr(logger, "debug", None)
        if callable(f):
            f(msg)
    except Exception:
        return


@register("astrbot_plugin_magnetic_link_analysis", "anonymous", "磁链解析插件（whatslink.info）", "1.0.0")
class WhatslinkPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self._tmp_files: List[str] = []
        self._session: aiohttp.ClientSession | None = None
        self._net_sem: asyncio.Semaphore | None = None
        self._net_limit: int | None = None
        self._rate: dict[str, collections.deque[float]] = {}

    async def initialize(self):
        """异步初始化（可选）"""
        await self._ensure_session()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    def _ensure_net_semaphore(self, limit: int) -> asyncio.Semaphore:
        try:
            limit = int(limit)
        except Exception:
            limit = 4
        if limit < 1:
            limit = 1
        if limit > 32:
            limit = 32
        if self._net_sem is None or self._net_limit != limit:
            self._net_sem = asyncio.Semaphore(limit)
            self._net_limit = limit
        return self._net_sem

    def _consume_rate(self, key: str, cost: int, limit: int, window_sec: float) -> bool:
        try:
            cost = int(cost)
        except Exception:
            cost = 1
        if cost < 1:
            cost = 1
        try:
            limit = int(limit)
        except Exception:
            limit = 10
        if limit < 1:
            limit = 1
        try:
            window_sec = float(window_sec)
        except Exception:
            window_sec = 60.0
        if window_sec <= 0:
            window_sec = 60.0

        now = time.monotonic()
        dq = self._rate.get(key)
        if dq is None:
            dq = collections.deque()
            self._rate[key] = dq
        while dq and now - dq[0] > window_sec:
            dq.popleft()
        if len(dq) + cost > limit:
            return False
        for _ in range(cost):
            dq.append(now)
        return True

    def _parse_host_allowlist(self, s: str | None) -> set[str] | None:
        if not s:
            return None
        hosts = set()
        for part in str(s).split(","):
            h = part.strip().lower()
            if h:
                hosts.add(h)
        return hosts or None

    def _is_safe_http_url(self, url: str, host_allowlist: set[str] | None = None) -> bool:
        try:
            p = urllib.parse.urlparse(url)
        except Exception:
            return False
        scheme = (p.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False
        host = (p.hostname or "").strip().lower()
        if not host:
            return False
        if host in ("localhost",) or host.endswith(".local"):
            return False
        if host_allowlist is not None:
            if host not in host_allowlist and not any(host.endswith("." + h) for h in host_allowlist):
                return False
        try:
            ip = ipaddress.ip_address(host)
        except Exception:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
        return True

    def _cleanup_tmp_files_from(self, start_index: int):
        if start_index < 0:
            start_index = 0
        new_files = self._tmp_files[start_index:]
        if not new_files:
            return
        keep: List[str] = self._tmp_files[:start_index]
        for p in new_files:
            try:
                os.remove(p)
            except Exception:
                keep.append(p)
        self._tmp_files = keep

    def _build_image_from_bytes(self, data: bytes, suffix: str = ".png"):
        if not data:
            return None

        from_bytes = _first_callable(Image, ["fromBytes", "from_bytes"])
        if from_bytes:
            try:
                return from_bytes(data)
            except Exception:
                _log_debug("Image.fromBytes 失败")

        from_base64 = _first_callable(Image, ["fromBase64", "from_base64"])
        if from_base64:
            try:
                b64 = base64.b64encode(data).decode("ascii")
                return from_base64(b64)
            except Exception:
                _log_debug("Image.fromBase64 失败")

        from_file = _first_callable(Image, ["fromFile", "from_file", "fromPath", "from_path"])
        if from_file:
            try:
                fd, path = tempfile.mkstemp(prefix="astrbot_whatslink_", suffix=suffix)
                os.close(fd)
                with open(path, "wb") as f:
                    f.write(data)
                self._tmp_files.append(path)
                return from_file(path)
            except Exception:
                logger.warning("创建临时截图文件失败")
                return None

        return None

    async def _fetch_bytes(self, url: str, timeout_ms: int = 10000) -> bytes | None:
        session = await self._ensure_session()
        sem = self._ensure_net_semaphore(self._net_limit or 4)
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 if timeout_ms else None)
        try:
            async with sem:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.read()
        except Exception as e:
            _log_debug(f"下载截图失败: {type(e).__name__}")
            return None

    def _add_noise_to_image_bytes(self, data: bytes, strength: int, ratio: float) -> bytes | None:
        try:
            from PIL import Image as PILImage
        except Exception:
            return None

        try:
            img = PILImage.open(io.BytesIO(data))
            img.load()
            img = img.convert("RGB")
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
            n = int(w * h * ratio)
            if n < 1:
                n = 1
            px = img.load()
            for _ in range(n):
                x = random.randrange(w)
                y = random.randrange(h)
                r, g, b = px[x, y]
                r = max(0, min(255, r + random.randint(-strength, strength)))
                g = max(0, min(255, g + random.randint(-strength, strength)))
                b = max(0, min(255, b + random.randint(-strength, strength)))
                px[x, y] = (r, g, b)
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
        except Exception as e:
            logger.warning(f"截图加噪失败: {e}")
            return None

    async def _make_screenshot_component(
        self,
        url: str,
        timeout_ms: int,
        enable_noise: bool,
        noise_strength: int,
        noise_ratio: float,
        host_allowlist: set[str] | None = None,
    ):
        if not self._is_safe_http_url(url, host_allowlist=host_allowlist):
            logger.warning("截图 URL 被拦截（不安全）")
            return None

        if not enable_noise:
            return Image.fromURL(url)

        if _first_callable(Image, ["fromBytes", "from_bytes", "fromBase64", "from_base64", "fromFile", "from_file", "fromPath", "from_path"]) is None:
            return Image.fromURL(url)

        data = await self._fetch_bytes(url, timeout_ms=timeout_ms)
        if not data:
            return Image.fromURL(url)

        noisy = self._add_noise_to_image_bytes(data, strength=noise_strength, ratio=noise_ratio)
        if noisy:
            img = self._build_image_from_bytes(noisy, suffix=".png")
            if img is not None:
                return img

        img = self._build_image_from_bytes(data, suffix=".png")
        if img is not None:
            return img

        return Image.fromURL(url)

    async def _call_api(self, url: str, timeout_ms: int = 10000) -> dict | None:
        """调用 whatslink.info API 并返回 JSON，失败返回 None。"""
        session = await self._ensure_session()
        sem = self._ensure_net_semaphore(self._net_limit or 4)
        q = {"url": url}
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 if timeout_ms else None)
        try:
            async with sem:
                async with session.get(API_URL, params=q, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.error(f"whatslink.info 返回状态码: {resp.status}")
                        return None
                    data = await resp.json()
                    return data
        except asyncio.TimeoutError:
            logger.warning("whatslink.info 请求超时")
            return None
        except Exception as e:
            logger.error(f"whatslink.info 请求出错: {e}")
            return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，自动识别并解析磁链（magnet:）。

        行为：
        - 当消息中包含 magnet 链接时，触发解析流程。
        - 先发送一条“解析中...”提示（平台不一定支持撤回，本插件会尽量减少噪音）。
        - 请求 API，格式化并发送解析结果；根据配置可发送合并转发（QQ/OneBot）。
        """
        text = event.get_message_str() or ""
        if not text:
            return

        magnets = MAGNET_RE.findall(text)
        if not magnets:
            return

        # 读取配置：从 AstrBot 全局配置的 plugin_settings 下读取本插件的配置
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        plugin_settings = cfg.get("plugin_settings", {})
        plugin_cfg = plugin_settings.get("astrbot_plugin_magnetic_link_analysis")
        if not isinstance(plugin_cfg, dict):
            plugin_cfg = plugin_settings.get("astrbot_plugin_whatslinkInfo", {})
        if not isinstance(plugin_cfg, dict):
            plugin_cfg = {}
        try:
            timeout = int(plugin_cfg.get("timeout", 10000))
        except Exception:
            timeout = 10000
        use_forward = bool(plugin_cfg.get("useForward", True))
        show_screenshot = bool(plugin_cfg.get("showScreenshot", True))
        noise_screenshot = bool(plugin_cfg.get("noiseScreenshot", True))
        try:
            max_magnets = int(plugin_cfg.get("maxMagnetsPerMessage", 3))
        except Exception:
            max_magnets = 3
        if max_magnets < 1:
            max_magnets = 1
        if max_magnets > 20:
            max_magnets = 20
        magnets = magnets[:max_magnets]
        try:
            max_concurrent = int(plugin_cfg.get("maxConcurrentRequests", 4))
        except Exception:
            max_concurrent = 4
        self._ensure_net_semaphore(max_concurrent)
        try:
            rate_limit = int(plugin_cfg.get("rateLimitCount", 10))
        except Exception:
            rate_limit = 10
        try:
            rate_window = float(plugin_cfg.get("rateLimitWindowSec", 60))
        except Exception:
            rate_window = 60.0
        host_allowlist = self._parse_host_allowlist(plugin_cfg.get("screenshotHostAllowlist"))
        rate_key = f"{event.get_platform_name()}:{event.get_sender_id()}"
        if not self._consume_rate(rate_key, cost=len(magnets), limit=rate_limit, window_sec=rate_window):
            try:
                yield event.plain_result("请求过于频繁，请稍后再试")
            except Exception:
                pass
            return
        try:
            noise_strength = int(plugin_cfg.get("noiseStrength", 8))
        except Exception:
            noise_strength = 8
        try:
            noise_ratio = float(plugin_cfg.get("noiseRatio", 0.002))
        except Exception:
            noise_ratio = 0.002
        if noise_strength < 1:
            noise_strength = 1
        if noise_strength > 50:
            noise_strength = 50
        if noise_ratio <= 0:
            noise_ratio = 0.002
        if noise_ratio > 0.05:
            noise_ratio = 0.05

        # 先发“解析中”的提示（尽量简短）
        try:
            yield event.plain_result("解析磁链中...")
        except Exception:
            _log_debug("发送“解析中”提示失败")

        tmp_start = len(self._tmp_files)
        results_to_send: List[MessageEventResult] = []

        try:
            for m in magnets:
                api_ret = await self._call_api(m, timeout_ms=timeout)
                if not api_ret:
                    r = MessageEventResult().message(f"解析失败: {m}")
                    results_to_send.append(r)
                    continue

                err = api_ret.get("error") or ""
                if err:
                    results_to_send.append(MessageEventResult().message(f"解析失败: {err}"))
                    continue

                name = api_ret.get("name", "未知名称")
                size = api_ret.get("size")
                count = api_ret.get("count")
                screenshots = api_ret.get("screenshots", []) or []

                header = f"名称: {name}\n文件数量: {count}\n总大小: {size} ({_human_readable_size(size)})\n"

                shots: List[str] = []
                if show_screenshot and isinstance(screenshots, list) and len(screenshots) > 0:
                    for s in screenshots:
                        url = s.get("screenshot")
                        if url:
                            shots.append(url)

                if use_forward and event.get_platform_name() in ("aiocqhttp", "qq", "qq_official", "onebot"):
                    content = [Plain(header)]
                    for url in shots:
                        comp = await self._make_screenshot_component(
                            url=url,
                            timeout_ms=timeout,
                            enable_noise=noise_screenshot,
                            noise_strength=noise_strength,
                            noise_ratio=noise_ratio,
                            host_allowlist=host_allowlist,
                        )
                        if comp is not None:
                            content.append(comp)
                    node = Node(content=content, name=event.get_sender_name(), uin=str(event.get_sender_id()))
                    nodes = Nodes(nodes=[node])
                    mer = MessageEventResult()
                    mer.chain = [nodes]
                    results_to_send.append(mer)
                else:
                    mer = MessageEventResult().message(header)
                    for url in shots:
                        comp = await self._make_screenshot_component(
                            url=url,
                            timeout_ms=timeout,
                            enable_noise=noise_screenshot,
                            noise_strength=noise_strength,
                            noise_ratio=noise_ratio,
                            host_allowlist=host_allowlist,
                        )
                        if comp is not None:
                            mer.chain.append(comp)
                    results_to_send.append(mer)

            for r in results_to_send:
                try:
                    await self.context.send_message(event.unified_msg_origin, r)
                except Exception as e:
                    logger.error(f"发送解析结果失败: {e}")
        finally:
            self._cleanup_tmp_files_from(tmp_start)

    async def terminate(self):
        """插件被卸载/停用时调用（可选）"""
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        for p in self._tmp_files:
            try:
                os.remove(p)
            except Exception:
                pass
        self._tmp_files = []
        return
