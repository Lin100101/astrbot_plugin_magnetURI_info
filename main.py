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
import hashlib
from typing import List, Optional, Dict
from dataclasses import dataclass
from datetime import datetime, timedelta

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
BTIH_MD5_LENGTH = 32
BTIH_SHA1_LENGTH = 40
BTIH_RAW_MIN_LEN = 16
BTIH_RAW_MAX_LEN = 160
MAGNET_MIN_COMPACT_LEN = 10


def _normalize_magnet_candidate(raw: str) -> str | None:
    if not raw:
        return None

    m = re.search(
        rf"urn:btih:([A-Za-z0-9%._\-\s]{{{BTIH_RAW_MIN_LEN},{BTIH_RAW_MAX_LEN}}})",
        raw,
        re.IGNORECASE,
    )
    if not m:
        return None

    clean_hash = re.sub(r"[^A-Za-z0-9]", "", m.group(1))
    if len(clean_hash) not in (BTIH_MD5_LENGTH, BTIH_SHA1_LENGTH):
        return None

    hash_start, hash_end = m.span(1)
    rebuilt = f"{raw[:hash_start]}{clean_hash}{raw[hash_end:]}"
    return re.sub(r"\s+", "", rebuilt)


def extract_magnets(text: str, max_len: int = 200) -> list[str]:
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for m in re.finditer(r"magnet:", text, re.IGNORECASE):
        chunk = text[m.start() : m.start() + max_len]
        candidates: list[str] = []
        short = re.match(rf"magnet:[^\s]{{{MAGNET_MIN_COMPACT_LEN},}}", chunk, re.IGNORECASE)
        if short:
            candidates.append(short.group(0))
        candidates.append(chunk)

        for cand in candidates:
            norm = _normalize_magnet_candidate(cand)
            if norm and norm not in seen:
                seen.add(norm)
                found.append(norm)

    return found


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

@dataclass
class CacheEntry:
    data: dict | bytes
    timestamp: datetime
    hit_count: int = 0

class SmartCache:
    def __init__(self, default_ttl: int = 300, max_size: int = 1000):
        self._cache: Dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
    
    def _generate_key(self, url_or_magnet: str) -> str:
        match = re.search(r'urn:btih:([A-Za-z0-9]{32,40})', url_or_magnet, re.IGNORECASE)
        if match:
            return match.group(1).lower()
        return hashlib.md5(url_or_magnet.encode()).hexdigest()
    
    def get(self, key_str: str) -> Optional[dict | bytes]:
        key = self._generate_key(key_str)
        entry = self._cache.get(key)
        
        if entry and datetime.now() - entry.timestamp < timedelta(seconds=self._default_ttl):
            entry.hit_count += 1
            self._hits += 1
            return entry.data
        
        if entry:
            del self._cache[key]
            
        self._misses += 1
        return None
    
    def set(self, key_str: str, data: dict | bytes):
        key = self._generate_key(key_str)
        
        if len(self._cache) >= self._max_size and key not in self._cache:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k].timestamp)
            del self._cache[oldest_key]
        
        self._cache[key] = CacheEntry(data=data, timestamp=datetime.now())

class RetryManager:
    def __init__(self):
        self.failure_counts = {}
        self.circuit_breakers = {}
    
    async def execute_with_retry(
        self, 
        func, 
        *args,
        max_retries: int = 2,
        base_delay: float = 0.2,
        max_delay: float = 2.0,
        key: str = "default",
        **kwargs
    ):
        if self._is_circuit_open(key):
            raise Exception(f"Circuit breaker open for {key}")
        
        for attempt in range(max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                
                if key in self.failure_counts:
                    del self.failure_counts[key]
                
                return result
                
            except Exception as e:
                self.failure_counts[key] = self.failure_counts.get(key, 0) + 1
                
                if self.failure_counts[key] >= 5:
                    self._open_circuit(key)
                
                if attempt == max_retries:
                    raise e
                
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.uniform(0, delay * 0.1)
                
                logger.warning(f"请求失败，{delay:.2f}秒后重试 (attempt {attempt + 1})")
                await asyncio.sleep(delay)
    
    def _is_circuit_open(self, key: str) -> bool:
        if key not in self.circuit_breakers:
            return False
        
        open_time, timeout = self.circuit_breakers[key]
        if datetime.now() - open_time > timedelta(seconds=timeout):
            del self.circuit_breakers[key]
            return False
        
        return True
    
    def _open_circuit(self, key: str, timeout: int = 60):
        self.circuit_breakers[key] = (datetime.now(), timeout)
        logger.error(f"熔断器开启 for {key}, timeout: {timeout}s")

class _SafeResolver(aiohttp.DefaultResolver):
    def __init__(self, host_allowlist: set[str] | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.host_allowlist = host_allowlist

    async def resolve(self, host: str, port: int, family: int = 0) -> list[dict]:
        host_lower = host.lower()
        if host_lower in ("localhost",) or host_lower.endswith(".local"):
            raise OSError(f"Refused to resolve private host: {host}")
        
        if self.host_allowlist is not None:
            if host_lower not in self.host_allowlist and not any(host_lower.endswith("." + h) for h in self.host_allowlist):
                raise OSError(f"Host not in allowlist: {host}")

        ips = await super().resolve(host, port, family)
        
        for info in ips:
            try:
                ip_str = info.get("host")
                if not ip_str:
                    continue
                ip_obj = ipaddress.ip_address(ip_str)
                if (
                    ip_obj.is_private
                    or ip_obj.is_loopback
                    or ip_obj.is_link_local
                    or ip_obj.is_multicast
                    or ip_obj.is_reserved
                    or ip_obj.is_unspecified
                ):
                    raise OSError(f"Resolved to private IP: {ip_str}")
            except ValueError:
                pass
        return ips

class PerformanceMonitor:
    def __init__(self):
        self.metrics = {
            'api_calls': 0,
            'api_errors': 0,
            'screenshot_downloads': 0,
            'total_response_time': 0.0,
            'cache_hits': 0,
            'cache_misses': 0,
            'start_time': datetime.now()
        }
        self._lock = asyncio.Lock()
    
    async def record_api_call(self, duration: float, success: bool):
        async with self._lock:
            self.metrics['api_calls'] += 1
            self.metrics['total_response_time'] += duration
            if not success:
                self.metrics['api_errors'] += 1
    
    async def record_cache_operation(self, hit: bool):
        async with self._lock:
            if hit:
                self.metrics['cache_hits'] += 1
            else:
                self.metrics['cache_misses'] += 1
    
    async def record_screenshot_download(self):
        async with self._lock:
            self.metrics['screenshot_downloads'] += 1

    def get_stats(self) -> dict:
        total_requests = self.metrics['api_calls']
        avg_response_time = (self.metrics['total_response_time'] / max(1, total_requests))
        total_cache_ops = self.metrics['cache_hits'] + self.metrics['cache_misses']
        cache_hit_rate = self.metrics['cache_hits'] / max(1, total_cache_ops)
        
        return {
            'total_api_calls': total_requests,
            'api_error_rate': self.metrics['api_errors'] / max(1, total_requests),
            'avg_response_time_ms': avg_response_time * 1000,
            'cache_hit_rate': cache_hit_rate,
            'screenshot_downloads': self.metrics['screenshot_downloads'],
            'uptime_hours': (datetime.now() - self.metrics['start_time']).total_seconds() / 3600
        }

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
        self._rate_limits: dict[str, tuple[int, float]] = {}
        self._last_rate_cleanup: float = 0.0
        self._api_cache = SmartCache(default_ttl=300, max_size=1000)
        self._screenshot_cache = SmartCache(default_ttl=600, max_size=500)
        self._retry_manager = RetryManager()
        self._monitor = PerformanceMonitor()

    async def initialize(self):
        """异步初始化（可选）"""
        await self._ensure_session()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
            
        connector = aiohttp.TCPConnector(
            limit=100,              
            limit_per_host=30,      
            ttl_dns_cache=300,      
            keepalive_timeout=30,   
            use_dns_cache=True,     
            enable_cleanup_closed=True,  
        )
        timeout = aiohttp.ClientTimeout(
            total=30,               
            connect=10,             
            sock_read=20            
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            trust_env=False,        
            headers={
                'User-Agent': 'AstrBot-MagnetParser/1.0',
                'Accept': 'application/json, text/plain;q=0.9',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            }
        )
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

        if key not in self._rate_limits:
            self._rate_limits[key] = (limit, window_sec)
        else:
            limit, window_sec = self._rate_limits[key]

        if now - self._last_rate_cleanup > 60.0:
            self._last_rate_cleanup = now
            keys_to_remove = []
            for k, dq_item in self._rate.items():
                _, k_window_sec = self._rate_limits.get(k, (10, 60.0))
                while dq_item and now - dq_item[0] > k_window_sec:
                    dq_item.popleft()
                if not dq_item:
                    keys_to_remove.append(k)
            for k in keys_to_remove:
                self._rate.pop(k, None)
                self._rate_limits.pop(k, None)

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
                os.chmod(path, 0o600)
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
        sem = self._ensure_net_semaphore(self._net_limit or 4)
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 if timeout_ms else None)
        
        resolver = _SafeResolver(host_allowlist=host_allowlist)
        connector = aiohttp.TCPConnector(resolver=resolver)
        async with aiohttp.ClientSession(connector=connector, trust_env=True) as safe_session:
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
                        p = urllib.parse.urlparse(cur_url)
                        if (p.scheme or "").lower() not in ("http", "https"):
                            return None
                        async with sem:
                            async with safe_session.get(cur_url, timeout=timeout, allow_redirects=False) as resp:
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
                except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                    _log_debug(f"下载截图失败: {type(e).__name__}")
                except Exception as e:
                    _log_debug(f"下载截图失败: {type(e).__name__}")
                    return None

                if attempt < retries:
                    await self._sleep_backoff(attempt, base_delay_ms=retry_base_delay_ms)

        return None

    async def _add_noise_to_image_bytes(self, data: bytes, strength: int, ratio: float, max_pixels: int) -> bytes | None:
        def _process():
            try:
                from PIL import Image as PILImage
                from PIL import ImageFile
            except Exception:
                return None
            DecompressionBombError = getattr(PILImage, "DecompressionBombError", None)

            try:
                try:
                    mp = int(max_pixels)
                except Exception:
                    mp = 20_000_000
                
                ImageFile.LOAD_TRUNCATED_IMAGES = False
                if mp > 0:
                    PILImage.MAX_IMAGE_PIXELS = mp
                
                with io.BytesIO(data) as bio:
                    with PILImage.open(bio) as img:
                        w, h = img.size
                        if w <= 0 or h <= 0:
                            return None
                        if mp > 0 and w * h > mp:
                            return None
                        
                        if img.format not in ['JPEG', 'PNG', 'WEBP']:
                            return None
                            
                        img.load()
                        if img.mode != 'RGB':
                            img = img.convert('RGB')
                            
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
        
        return await asyncio.to_thread(_process)

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
        cached = self._screenshot_cache.get(url)
        if cached is not None and isinstance(cached, bytes):
            await self._monitor.record_cache_operation(hit=True)
            data = cached
        else:
            await self._monitor.record_cache_operation(hit=False)
            data = await self._fetch_bytes(
                url,
                timeout_ms=timeout_ms,
                host_allowlist=host_allowlist,
                max_bytes=max_bytes,
                max_redirects=max_redirects,
                retries=retries,
                retry_base_delay_ms=retry_base_delay_ms,
            )
            if data:
                await self._monitor.record_screenshot_download()
                self._screenshot_cache.set(url, data)

        if not data:
            return None

        if enable_noise:
            noisy = await self._add_noise_to_image_bytes(data, strength=noise_strength, ratio=noise_ratio, max_pixels=max_pixels)
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
        cached = self._api_cache.get(url)
        if cached is not None and isinstance(cached, dict):
            await self._monitor.record_cache_operation(hit=True)
            return cached
        await self._monitor.record_cache_operation(hit=False)

        session = await self._ensure_session()
        sem = self._ensure_net_semaphore(self._net_limit or 4)
        q = {"url": url}
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 if timeout_ms else None)

        if retries < 0:
            retries = 0
        if retries > 2:
            retries = 2

        async def _do_request():
            async with sem:
                start_time = time.monotonic()
                try:
                    async with session.get(API_URL, params=q, timeout=timeout) as resp:
                        duration = time.monotonic() - start_time
                        if resp.status == 200:
                            data = await resp.json()
                            if data:
                                self._api_cache.set(url, data)
                            await self._monitor.record_api_call(duration, success=True)
                            return data
                        if 500 <= resp.status <= 599:
                            await self._monitor.record_api_call(duration, success=False)
                            raise Exception(f"Server error: {resp.status}")
                        else:
                            await self._monitor.record_api_call(duration, success=False)
                            logger.warning("外部服务请求失败")
                            return None
                except Exception:
                    duration = time.monotonic() - start_time
                    await self._monitor.record_api_call(duration, success=False)
                    raise
                        
        try:
            return await self._retry_manager.execute_with_retry(
                _do_request,
                max_retries=retries,
                base_delay=retry_base_delay_ms / 1000.0,
                max_delay=5.0,
                key=f"api_{API_URL}"
            )
        except Exception as e:
            logger.warning("外部服务请求失败")
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
            v = plugin_cfg.get(key)
            if v is None:
                return default
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                v_lower = v.lower().strip()
                if v_lower in ("true", "1", "yes", "on", "t", "y"):
                    return True
                if v_lower in ("false", "0", "no", "off", "f", "n"):
                    return False
            return bool(v)

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

    async def _fetch_screenshot_components(self, shots: List[str], cfg: dict) -> List[Image]:
        if not shots:
            return []
            
        optimal_concurrency = min(
            cfg["max_concurrent"],
            len(shots),
            max(1, len(shots) // 2)
        )
        semaphore = asyncio.Semaphore(optimal_concurrency)

        async def process_with_semaphore(url: str):
            async with semaphore:
                return await self._make_screenshot_component(
                    url=url,
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

        tasks = [process_with_semaphore(u) for u in shots]
        
        results = []
        comps = await asyncio.gather(*tasks, return_exceptions=True)
        for c in comps:
            if isinstance(c, Exception):
                _log_debug(f"截图处理失败: {type(c).__name__}")
                continue
            if c is not None:
                results.append(c)
        return results

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
            comps = await self._fetch_screenshot_components(shots, cfg)
            content.extend(comps)
            node = Node(content=content, name=event.get_sender_name(), uin=str(event.get_sender_id()))
            nodes = Nodes(nodes=[node])
            mer = MessageEventResult()
            mer.chain = [nodes]
            return mer

        mer = MessageEventResult().message(header)
        comps = await self._fetch_screenshot_components(shots, cfg)
        mer.chain.extend(comps)
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

        magnets = extract_magnets(text)
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
