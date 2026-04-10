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
        self._last_rate_cleanup: float = 0.0

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
        if self._net_sem is None:
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

        if now - self._last_rate_cleanup > 60.0:
            self._last_rate_cleanup = now
            keys_to_remove = []
            for k, dq_item in self._rate.items():
                while dq_item and now - dq_item[0] > window_sec:
                    dq_item.popleft()
                if not dq_item:
                    keys_to_remove.append(k)
            for k in keys_to_remove:
                self._rate.pop(k, None)

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

    async def _is_safe_http_url(self, url: str, host_allowlist: set[str] | None = None) -> bool:
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
            ips = [ip]
        except ValueError:
            try:
                loop = asyncio.get_running_loop()
                addr_info = await loop.getaddrinfo(host, None)
                ips = []
                for info in addr_info:
                    try:
                        ips.append(ipaddress.ip_address(info[4][0]))
                    except Exception:
                        pass
                if not ips:
                    return False
            except Exception:
                return False

        for ip_obj in ips:
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
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

    async def _sleep_backoff(self, attempt: int, base_delay_ms: int):
        try:
            attempt = int(attempt)
        except Exception:
            attempt = 0
        if attempt < 0:
            attempt = 0
        try:
            base_delay_ms = int(base_delay_ms)
        except Exception:
            base_delay_ms = 200
        if base_delay_ms < 0:
            base_delay_ms = 0
        delay = (base_delay_ms / 1000.0) * (2**attempt)
        delay += random.random() * 0.1
        if delay > 0:
            await asyncio.sleep(delay)

    async def _fetch_bytes(
        self,
        url: str,
        timeout_ms: int = 10000,
        *,
        host_allowlist: set[str] | None = None,
        max_bytes: int = 8 * 1024 * 1024,
        max_redirects: int = 3,
        retries: int = 1,
        retry_base_delay_ms: int = 200,
    ) -> bytes | None:
        session = await self._ensure_session()
        sem = self._ensure_net_semaphore(self._net_limit or 4)
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 if timeout_ms else None)
        try:
            max_bytes = int(max_bytes)
        except Exception:
            max_bytes = 8 * 1024 * 1024
        if max_bytes < 64 * 1024:
            max_bytes = 64 * 1024
        if max_bytes > 50 * 1024 * 1024:
            max_bytes = 50 * 1024 * 1024
        try:
            max_redirects = int(max_redirects)
        except Exception:
            max_redirects = 3
        if max_redirects < 0:
            max_redirects = 0
        if max_redirects > 10:
            max_redirects = 10
        try:
            retries = int(retries)
        except Exception:
            retries = 1
        if retries < 0:
            retries = 0
        if retries > 2:
            retries = 2

        for attempt in range(retries + 1):
            cur_url = url
            try:
                for _ in range(max_redirects + 1):
                    if not await self._is_safe_http_url(cur_url, host_allowlist=host_allowlist):
                        return None
                    async with sem:
                        async with session.get(cur_url, timeout=timeout, allow_redirects=False) as resp:
                            if resp.status in (301, 302, 303, 307, 308):
                                loc = resp.headers.get("Location")
                                if not loc:
                                    return None
                                cur_url = urllib.parse.urljoin(cur_url, loc)
                                continue
                            if resp.status == 200:
                                cl = resp.headers.get("Content-Length")
                                if cl:
                                    try:
                                        if int(cl) > max_bytes:
                                            return None
                                    except Exception:
                                        pass
                                buf = bytearray()
                                async for chunk in resp.content.iter_chunked(64 * 1024):
                                    if not chunk:
                                        continue
                                    buf.extend(chunk)
                                    if len(buf) > max_bytes:
                                        return None
                                return bytes(buf)
                            if 500 <= resp.status <= 599:
                                break
                            return None
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                _log_debug(f"下载截图失败: {type(e).__name__}")
            except Exception as e:
                _log_debug(f"下载截图失败: {type(e).__name__}")
                return None

            if attempt < retries:
                await self._sleep_backoff(attempt, base_delay_ms=retry_base_delay_ms)

        return None

    def _add_noise_to_image_bytes(self, data: bytes, strength: int, ratio: float, max_pixels: int) -> bytes | None:
        try:
            from PIL import Image as PILImage
        except Exception:
            return None
        DecompressionBombError = getattr(PILImage, "DecompressionBombError", None)

        try:
            img = PILImage.open(io.BytesIO(data))
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
            try:
                max_pixels = int(max_pixels)
            except Exception:
                max_pixels = 20_000_000
            if max_pixels > 0 and w * h > max_pixels:
                return None
            img.load()
            img = img.convert("RGB")
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
            if DecompressionBombError is not None and isinstance(e, DecompressionBombError):
                logger.warning("截图加噪失败: 图片像素过大")
                return None
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
        max_bytes: int = 8 * 1024 * 1024,
        max_redirects: int = 3,
        max_pixels: int = 20_000_000,
        retries: int = 1,
        retry_base_delay_ms: int = 200,
    ):
        if not await self._is_safe_http_url(url, host_allowlist=host_allowlist):
            logger.warning("截图 URL 被拦截（不安全）")
            return None

        data = await self._fetch_bytes(
            url,
            timeout_ms=timeout_ms,
            host_allowlist=host_allowlist,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
            retries=retries,
            retry_base_delay_ms=retry_base_delay_ms,
        )
        if not data:
            return None

        if enable_noise:
            noisy = self._add_noise_to_image_bytes(data, strength=noise_strength, ratio=noise_ratio, max_pixels=max_pixels)
            if noisy:
                img = self._build_image_from_bytes(noisy, suffix=".png")
                if img is not None:
                    return img

        img = self._build_image_from_bytes(data, suffix=".png")
        if img is not None:
            return img

        return None

    async def _call_api(self, url: str, timeout_ms: int = 10000, retries: int = 1, retry_base_delay_ms: int = 200) -> dict | None:
        """调用 whatslink.info API 并返回 JSON，失败返回 None。"""
        session = await self._ensure_session()
        sem = self._ensure_net_semaphore(self._net_limit or 4)
        q = {"url": url}
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 if timeout_ms else None)

        if retries < 0:
            retries = 0
        if retries > 2:
            retries = 2

        for attempt in range(retries + 1):
            try:
                async with sem:
                    async with session.get(API_URL, params=q, timeout=timeout) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        if 500 <= resp.status <= 599:
                            pass
                        else:
                            logger.error(f"whatslink.info 返回状态码: {resp.status}")
                            return None
            except asyncio.TimeoutError:
                logger.warning("whatslink.info 请求超时")
            except aiohttp.ClientError as e:
                logger.warning(f"whatslink.info 请求出错: {type(e).__name__}")
            except Exception as e:
                logger.error(f"whatslink.info 请求出错: {e}")
                return None

            if attempt < retries:
                await self._sleep_backoff(attempt, base_delay_ms=retry_base_delay_ms)

        return None

    def _parse_config(self, event: AstrMessageEvent) -> dict:
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        plugin_settings = cfg.get("plugin_settings", {})
        plugin_cfg = plugin_settings.get("astrbot_plugin_magnetic_link_analysis")
        if not isinstance(plugin_cfg, dict):
            plugin_cfg = plugin_settings.get("astrbot_plugin_whatslinkInfo", {})
        if not isinstance(plugin_cfg, dict):
            plugin_cfg = {}
            
        def _get_int(key: str, default: int) -> int:
            try:
                return int(plugin_cfg.get(key, default))
            except Exception:
                return default
                
        def _get_float(key: str, default: float) -> float:
            try:
                return float(plugin_cfg.get(key, default))
            except Exception:
                return default
                
        def _get_bool(key: str, default: bool) -> bool:
            return bool(plugin_cfg.get(key, default))

        return {
            "timeout": _get_int("timeout", 10000),
            "use_forward": _get_bool("useForward", True),
            "show_screenshot": _get_bool("showScreenshot", True),
            "noise_screenshot": _get_bool("noiseScreenshot", True),
            "max_magnets": max(1, min(20, _get_int("maxMagnetsPerMessage", 3))),
            "max_concurrent": _get_int("maxConcurrentRequests", 4),
            "max_screenshots": max(0, min(10, _get_int("maxScreenshotsPerMagnet", 3))),
            "max_screenshot_bytes": _get_int("maxScreenshotBytes", 8 * 1024 * 1024),
            "max_screenshot_redirects": _get_int("maxScreenshotRedirects", 3),
            "max_screenshot_pixels": _get_int("maxScreenshotPixels", 20_000_000),
            "request_retries": _get_int("requestRetries", 1),
            "retry_base_delay_ms": _get_int("requestRetryBaseDelayMs", 200),
            "rate_limit": _get_int("rateLimitCount", 10),
            "rate_window": _get_float("rateLimitWindowSec", 60.0),
            "host_allowlist": self._parse_host_allowlist(plugin_cfg.get("screenshotHostAllowlist")),
            "noise_strength": max(1, min(50, _get_int("noiseStrength", 8))),
            "noise_ratio": max(0.002, min(0.05, _get_float("noiseRatio", 0.002))),
        }

    async def _process_magnet(self, magnet: str, cfg: dict, event: AstrMessageEvent) -> MessageEventResult:
        api_ret = await self._call_api(
            magnet, 
            timeout_ms=cfg["timeout"], 
            retries=cfg["request_retries"], 
            retry_base_delay_ms=cfg["retry_base_delay_ms"]
        )
        if not api_ret:
            return MessageEventResult().message(f"解析失败: {magnet}")

        err = api_ret.get("error") or ""
        if err:
            return MessageEventResult().message(f"解析失败: {err}")

        name = api_ret.get("name", "未知名称")
        size = api_ret.get("size")
        count = api_ret.get("count")
        screenshots = api_ret.get("screenshots", []) or []

        header = f"名称: {name}\n文件数量: {count}\n总大小: {size} ({_human_readable_size(size)})\n"

        shots: List[str] = []
        if (
            cfg["show_screenshot"]
            and cfg["max_screenshots"] > 0
            and isinstance(screenshots, list)
            and len(screenshots) > 0
        ):
            for s in screenshots:
                url = s.get("screenshot")
                if url:
                    shots.append(url)
                if len(shots) >= cfg["max_screenshots"]:
                    break

        if cfg["use_forward"] and event.get_platform_name() in ("aiocqhttp", "qq", "qq_official", "onebot"):
            content = [Plain(header)]
            tasks = [
                self._make_screenshot_component(
                    url=u,
                    timeout_ms=cfg["timeout"],
                    enable_noise=cfg["noise_screenshot"],
                    noise_strength=cfg["noise_strength"],
                    noise_ratio=cfg["noise_ratio"],
                    host_allowlist=cfg["host_allowlist"],
                    max_bytes=cfg["max_screenshot_bytes"],
                    max_redirects=cfg["max_screenshot_redirects"],
                    max_pixels=cfg["max_screenshot_pixels"],
                    retries=cfg["request_retries"],
                    retry_base_delay_ms=cfg["retry_base_delay_ms"],
                )
                for u in shots
            ]
            if tasks:
                comps = await asyncio.gather(*tasks, return_exceptions=True)
                for c in comps:
                    if isinstance(c, Exception):
                        _log_debug(f"截图处理失败: {type(c).__name__}")
                        continue
                    if c is not None:
                        content.append(c)
            node = Node(content=content, name=event.get_sender_name(), uin=str(event.get_sender_id()))
            nodes = Nodes(nodes=[node])
            mer = MessageEventResult()
            mer.chain = [nodes]
            return mer

        mer = MessageEventResult().message(header)
        tasks = [
            self._make_screenshot_component(
                url=u,
                timeout_ms=cfg["timeout"],
                enable_noise=cfg["noise_screenshot"],
                noise_strength=cfg["noise_strength"],
                noise_ratio=cfg["noise_ratio"],
                host_allowlist=cfg["host_allowlist"],
                max_bytes=cfg["max_screenshot_bytes"],
                max_redirects=cfg["max_screenshot_redirects"],
                max_pixels=cfg["max_screenshot_pixels"],
                retries=cfg["request_retries"],
                retry_base_delay_ms=cfg["retry_base_delay_ms"],
            )
            for u in shots
        ]
        if tasks:
            comps = await asyncio.gather(*tasks, return_exceptions=True)
            for c in comps:
                if isinstance(c, Exception):
                    _log_debug(f"截图处理失败: {type(c).__name__}")
                    continue
                if c is not None:
                    mer.chain.append(c)
        return mer

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

        cfg = self._parse_config(event)
        
        magnets = magnets[:cfg["max_magnets"]]
        
        self._ensure_net_semaphore(cfg["max_concurrent"])
        
        rate_key = f"{event.get_platform_name()}:{event.get_sender_id()}"
        if not self._consume_rate(rate_key, cost=len(magnets), limit=cfg["rate_limit"], window_sec=cfg["rate_window"]):
            try:
                yield event.plain_result("请求过于频繁，请稍后再试")
            except Exception:
                pass
            return

        try:
            yield event.plain_result("解析磁链中...")
        except Exception:
            _log_debug("发送“解析中”提示失败")

        tmp_start = len(self._tmp_files)
        results_to_send: List[MessageEventResult] = []

        try:
            tasks = [self._process_magnet(m, cfg, event) for m in magnets]
            if tasks:
                built = await asyncio.gather(*tasks, return_exceptions=True)
                for r in built:
                    if isinstance(r, Exception):
                        logger.error(f"解析流程失败: {type(r).__name__}")
                        continue
                    results_to_send.append(r)

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
