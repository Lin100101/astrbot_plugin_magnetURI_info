import collections
import time
from typing import List
import aiohttp
import asyncio
import urllib.parse
import re

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

class ConfigManager:
    def __init__(self, context: Context):
        self.context = context
        self._rate: dict[str, collections.deque[float]] = {}
        self._rate_limits: dict[str, tuple[int, float]] = {}
        self._last_rate_cleanup: float = 0.0

    def parse_config(self, event: AstrMessageEvent) -> dict:
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        plugin_settings = cfg.get("plugin_settings", {})
        plugin_cfg = plugin_settings.get("astrbot_plugin_magnetURI_info")
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

        def _parse_host_allowlist(s: str | None) -> set[str] | None:
            if not s:
                return None
            hosts = set()
            for part in str(s).split(","):
                h = part.strip().lower()
                if h:
                    hosts.add(h)
            return hosts or None

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
            "host_allowlist": _parse_host_allowlist(plugin_cfg.get("screenshotHostAllowlist")),
            "noise_strength": max(1, min(50, _get_int("noiseStrength", 8))),
            "noise_ratio": max(0.002, min(0.05, _get_float("noiseRatio", 0.002))),
        }

    def consume_rate(self, key: str, cost: int, limit: int, window_sec: float) -> bool:
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
