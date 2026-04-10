import asyncio
import io
import os
import random
import tempfile
import base64
from typing import List, Optional
from astrbot.api import logger
from astrbot.api.message_components import Image

def _first_callable(obj, names: List[str]):
    for n in names:
        f = getattr(obj, n, None)
        if callable(f):
            return f
    return None

def build_image_from_bytes(data: bytes, suffix: str, tmp_files: List[str]) -> Optional[Image]:
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
            os.chmod(path, 0o600)
            with open(path, "wb") as f:
                f.write(data)
            tmp_files.append(path)
            return from_file(path)
        except Exception:
            logger.warning("创建临时截图文件失败")
            return None

    return None

async def add_noise_to_image_bytes(data: bytes, strength: int, ratio: float, max_pixels: int) -> bytes | None:
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