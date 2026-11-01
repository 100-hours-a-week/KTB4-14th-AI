"""사진 품질을 점수화하고 EXIF 방향·여백·흐린 배경을 처리해 720p로 맞춘다."""
from pathlib import Path
import warnings

import cv2
import numpy as np
from PIL import Image

from ai_service.services.video.errors import VideoError

MAX_PIXELS = 50_000_000


def visual_signature(pixels: np.ndarray) -> dict:
    """여백 없는 원본에서 pHash·색 분포를 계산해 중복과 시각적 다양성을 비교한다."""
    sample = cv2.resize(pixels, (128, 128), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
    spectrum = cv2.dct(cv2.resize(gray, (32, 32)).astype(np.float32))[:8, :8].flatten()
    bits = spectrum > np.median(spectrum[1:])
    bits[0] = False
    fingerprint = sum(int(bit) << i for i, bit in enumerate(bits))
    hsv = cv2.cvtColor(sample, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [12, 4], [0, 180, 0, 256]).flatten()
    histogram /= max(float(histogram.sum()), 1)
    return {'phash': f'{fingerprint:016x}', 'color_histogram': histogram.round(6).tolist()}


def assess_quality(pixels: np.ndarray) -> dict:
    """고정 분석 크기에서 초점·노출을 점수화한다. 의미나 인물의 감정을 분류하지 않는다."""
    h, w = pixels.shape[:2]
    scale = min(1.0, 1024 / max(h, w))
    sample = cv2.resize(pixels, (max(1, round(w * scale)), max(1, round(h * scale))),
                        interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    dark, bright = float(np.mean(gray < 20)), float(np.mean(gray > 235))
    contrast = float(np.percentile(gray, 95) - np.percentile(gray, 5))
    label = 'BLURRY' if sharpness < 60 else 'SOFT' if sharpness < 120 else 'SHARP'
    exposure = 'UNDEREXPOSED' if dark > 0.65 else 'OVEREXPOSED' if bright > 0.65 else 'NORMAL'
    warnings = [] if label == 'SHARP' else [label]
    if exposure != 'NORMAL':
        warnings.append(exposure)
    if contrast < 40:
        warnings.append('LOW_CONTRAST')
    if w < 640 or h < 360:
        warnings.append('LOW_RESOLUTION')
    return {'laplacian_variance': round(sharpness, 3), 'sharpness': label,
            'mean_luma': round(float(gray.mean()), 2), 'dark_fraction': round(dark, 4),
            'bright_fraction': round(bright, 4), 'exposure': exposure,
            'contrast_p95_p5': round(contrast, 2), 'warnings': warnings,
            'analysis_long_edge': max(sample.shape[:2]), 'calibration': 'heuristic_v1'}


def prepare_photo(source: Path, destination: Path, max_bytes: int, *, fit='letterbox') -> dict:
    try:
        if not source.is_file() or source.stat().st_size > max_bytes:
            raise VideoError("photo_missing_or_too_large")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                # iPhone JPEG가 보조 사진을 포함하면 Pillow는 MPO로 인식한다.
                # 일반 JPEG와 동일하게 첫 번째(주) 사진만 사용한다.
                if image.format not in {"JPEG", "MPO", "PNG", "WEBP"}:
                    raise VideoError("photo_format_unsupported")
                image.seek(0)
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
        quality = assess_quality(pixels)
        signature = visual_signature(pixels)
        oriented_h, oriented_w = pixels.shape[:2]
        scale = min(1280 / oriented_w, 720 / oriented_h)
        new_w, new_h = max(1, round(oriented_w * scale)), max(1, round(oriented_h * scale))
        resized = cv2.resize(pixels, (new_w, new_h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        if fit == 'cinematic_crop':
            if oriented_w / oriented_h > 1280 / 720:
                crop_w=max(1,round(oriented_h*1280/720));left=(oriented_w-crop_w)//2
                frame=pixels[:,left:left+crop_w]
            else:
                crop_h=max(1,round(oriented_w*720/1280));top=(oriented_h-crop_h)//2
                frame=pixels[top:top+crop_h,:]
            canvas=cv2.resize(frame,(1280,720),interpolation=cv2.INTER_AREA)
        elif fit == 'blurred_background':
            # 배경만 먼저 잘라 초광각·세로 파노라마도 거대한 중간 배열을 만들지 않는다.
            if oriented_w / oriented_h > 1280 / 720:
                crop_w = max(1, round(oriented_h * 1280 / 720))
                bx = (oriented_w - crop_w) // 2
                backdrop = pixels[:, bx:bx + crop_w]
            else:
                crop_h = max(1, round(oriented_w * 720 / 1280))
                by = (oriented_h - crop_h) // 2
                backdrop = pixels[by:by + crop_h, :]
            backdrop = cv2.resize(backdrop, (1280, 720))
            backdrop = cv2.GaussianBlur(backdrop, (0, 0), 30)
            canvas = np.clip(backdrop.astype(np.float32) * 0.65, 0, 255).astype(np.uint8)
        elif fit == 'letterbox':
            canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
        else:
            raise VideoError('photo_fit_invalid')
        x, y = (1280 - new_w) // 2, (720 - new_h) // 2
        if fit != 'cinematic_crop':
            canvas[y:y + new_h, x:x + new_w] = resized
        if not cv2.imwrite(str(destination), canvas):
            raise VideoError("photo_write_failed")
        return {"original_width": width, "original_height": height, "exif_orientation": orientation,
                "oriented_width": oriented_w, "oriented_height": oriented_h,
                "width": 1280, "height": 720, "fit": fit, 'quality': quality,
                'visual_signature': signature}
    except VideoError:
        raise
    except (OSError, ValueError, cv2.error, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise VideoError("photo_invalid") from exc
