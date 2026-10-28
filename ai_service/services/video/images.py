"""입력 사진을 검증하고 EXIF 방향 보정과 OpenCV 여백 처리를 거쳐 720p로 맞춘다."""
from pathlib import Path
import warnings

import cv2
import numpy as np
from PIL import Image

from ai_service.services.video.errors import VideoError

MAX_PIXELS = 50_000_000


def prepare_photo(source: Path, destination: Path, max_bytes: int) -> dict:
    try:
        if not source.is_file() or source.stat().st_size > max_bytes:
            raise VideoError("photo_missing_or_too_large")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise VideoError("photo_format_unsupported")
                width, height = image.size
                if width < 32 or height < 32 or width * height > MAX_PIXELS:
                    raise VideoError("photo_dimensions_invalid")
                orientation = image.getexif().get(274, 1)
            # PNG의 EXIF 조회가 디코더를 실행할 수 있어 검증할 때 파일을 새로 연다.
            with Image.open(source) as image:
                image.verify()
        # 운영체제별 디코더 기본 동작에 의존하지 않도록 EXIF 방향을 직접 보정한다.
        pixels = cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if pixels is None:
            raise VideoError("photo_decode_failed")
        if orientation == 2:
            pixels = cv2.flip(pixels, 1)
        elif orientation == 3:
            pixels = cv2.rotate(pixels, cv2.ROTATE_180)
        elif orientation == 4:
            pixels = cv2.flip(pixels, 0)
        elif orientation == 5:
            pixels = cv2.transpose(pixels)
        elif orientation == 6:
            pixels = cv2.rotate(pixels, cv2.ROTATE_90_CLOCKWISE)
        elif orientation == 7:
            pixels = cv2.flip(cv2.transpose(pixels), -1)
        elif orientation == 8:
            pixels = cv2.rotate(pixels, cv2.ROTATE_90_COUNTERCLOCKWISE)
        oriented_h, oriented_w = pixels.shape[:2]
        scale = min(1280 / oriented_w, 720 / oriented_h)
        new_w, new_h = max(1, round(oriented_w * scale)), max(1, round(oriented_h * scale))
        resized = cv2.resize(pixels, (new_w, new_h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
        x, y = (1280 - new_w) // 2, (720 - new_h) // 2
        canvas[y:y + new_h, x:x + new_w] = resized
        if not cv2.imwrite(str(destination), canvas):
            raise VideoError("photo_write_failed")
        return {"original_width": width, "original_height": height, "exif_orientation": orientation,
                "oriented_width": oriented_w, "oriented_height": oriented_h,
                "width": 1280, "height": 720, "fit": "letterbox"}
    except VideoError:
        raise
    except (OSError, ValueError, cv2.error, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise VideoError("photo_invalid") from exc
