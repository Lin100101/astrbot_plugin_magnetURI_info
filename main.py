"""
AstrBot 插件：whatslink.info 磁链解析器
- 自动识别消息中的 magnet: 链接
- 调用 https://whatslink.info/api/v1/link?url=... 获取资源信息
- 支持插件配置：timeout（毫秒），useForward（合并转发，QQ/OneBot），showScreenshot（显示截图）

中文注释已添加。
"""
from __future__ import annotations

import base64
import io
import os
import random
import re
import tempfile
import aiohttp
import asyncio
from typing import List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image, Node, Nodes


MAGNET_RE = re.compile(r"(magnet:\?xt=urn:btih:[A-Za-z0-9]+)", re.IGNORECASE)
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


@register("astrbot_plugin_whatslinkInfo", "anonymous", "磁链解析插件（whatslink.info）", "1.0.0")
class WhatslinkPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self._tmp_files: List[str] = []

    async def initialize(self):
        """异步初始化（可选）"""

    def _build_image_from_bytes(self, data: bytes, suffix: str = ".png"):
        if not data:
            return None

        from_bytes = _first_callable(Image, ["fromBytes", "from_bytes"])
        if from_bytes:
            try:
                return from_bytes(data)
            except Exception:
                pass

        from_base64 = _first_callable(Image, ["fromBase64", "from_base64"])
        if from_base64:
            try:
                b64 = base64.b64encode(data).decode("ascii")
                return from_base64(b64)
            except Exception:
                pass

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
                pass

        return None

    async def _fetch_bytes(self, url: str, timeout_ms: int = 10000) -> bytes | None:
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 if timeout_ms else None)
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.read()
        except Exception:
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
    ):
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
        q = {"url": url}
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000 if timeout_ms else None)
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
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
        plugin_cfg = cfg.get("plugin_settings", {}).get("astrbot_plugin_whatslinkInfo", {})
        timeout = int(plugin_cfg.get("timeout", 10000))
        use_forward = bool(plugin_cfg.get("useForward", True))
        show_screenshot = bool(plugin_cfg.get("showScreenshot", True))
        noise_screenshot = bool(plugin_cfg.get("noiseScreenshot", True))
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
            # 发送失败不影响主流程
            pass

        results_to_send: List[MessageEventResult] = []

        for m in magnets:
            api_ret = await self._call_api(m, timeout_ms=timeout)
            if not api_ret:
                # 请求失败
                r = MessageEventResult().message(f"解析失败: {m}")
                results_to_send.append(r)
                continue

            # 解析响应字段，按 API 文档处理
            err = api_ret.get("error") or ""
            if err:
                results_to_send.append(MessageEventResult().message(f"解析失败: {err}"))
                continue

            name = api_ret.get("name", "未知名称")
            size = api_ret.get("size")
            count = api_ret.get("count")
            file_type = api_ret.get("file_type", api_ret.get("type", ""))
            screenshots = api_ret.get("screenshots", []) or []

            # 构建要显示的文本：要求不展示类型和来源，仅显示名称、文件数量与总大小
            header = f"名称: {name}\n文件数量: {count}\n总大小: {size} ({_human_readable_size(size)})\n"

            # 如果需要显示截图，准备所有截图 URL 列表（按 API 返回顺序）。
            shots: List[str] = []
            if show_screenshot and isinstance(screenshots, list) and len(screenshots) > 0:
                for s in screenshots:
                    url = s.get("screenshot")
                    if url:
                        shots.append(url)

            # 构造 MessageEventResult
            if use_forward and event.get_platform_name() in ("aiocqhttp", "qq", "qq_official", "onebot"):
                # 对于 QQ/OneBot 平台，使用合并转发（Nodes）以避免刷屏。
                # Node 内容是一个消息链（列表），此处仅放入文本和可能的图片
                content = [Plain(header)]
                # 将所有截图附加为图片段
                for url in shots:
                    content.append(
                        await self._make_screenshot_component(
                            url=url,
                            timeout_ms=timeout,
                            enable_noise=noise_screenshot,
                            noise_strength=noise_strength,
                            noise_ratio=noise_ratio,
                        )
                    )
                node = Node(content=content, name=event.get_sender_name(), uin=str(event.get_sender_id()))
                nodes = Nodes(nodes=[node])
                mer = MessageEventResult()
                mer.chain = [nodes]
                results_to_send.append(mer)
            else:
                # 普通平台或不开启合并转发，直接发送文本 + 图片
                mer = MessageEventResult().message(header)
                for url in shots:
                    mer.chain.append(
                        await self._make_screenshot_component(
                            url=url,
                            timeout_ms=timeout,
                            enable_noise=noise_screenshot,
                            noise_strength=noise_strength,
                            noise_ratio=noise_ratio,
                        )
                    )
                results_to_send.append(mer)

        # 逐条发送解析结果。使用 context.send_message 主动发送，避免影响当前事件的传播控制。
        for r in results_to_send:
            try:
                await self.context.send_message(event.unified_msg_origin, r)
            except Exception as e:
                logger.error(f"发送解析结果失败: {e}")

    async def terminate(self):
        """插件被卸载/停用时调用（可选）"""
        for p in self._tmp_files:
            try:
                os.remove(p)
            except Exception:
                pass
        self._tmp_files = []
        return
