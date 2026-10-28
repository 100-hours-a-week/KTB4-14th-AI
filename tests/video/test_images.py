"""사진 형식·크기 검증과 EXIF 회전·반전 및 720p 전처리 결과를 검사한다."""
from pathlib import Path
import tempfile
import unittest

try:
    import cv2
    import numpy as np
    from PIL import Image
    from ai_service.services.video.images import prepare_photo
    VIDEO_EXTRAS = True
except ImportError:
    VIDEO_EXTRAS = False

from ai_service.services.video.errors import VideoError


@unittest.skipUnless(VIDEO_EXTRAS, "install uv sync --extra video --extra dev")
class PhotoTests(unittest.TestCase):
    def test_valid_png_and_webp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for extension in ("png", "webp"):
                source = root / f"input.{extension}"
                Image.new("RGB", (100, 200), "red").save(source)
                info = prepare_photo(source, root / "output.png", 100000)
                self.assertEqual((info["width"], info["height"]), (1280, 720))

    def test_portrait_and_all_exif_orientations_preserve_full_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for orientation in range(1, 9):
                # 색이 다른 영역으로 사진의 회전·반전 오류를 확인한다.
                image = Image.new("RGB", (80, 40), "red")
                image.paste("blue", (40, 0, 80, 40))
                exif = image.getexif()
                exif[274] = orientation
                source, output = root / "input.jpg", root / "prepared.png"
                image.save(source, exif=exif)
                info = prepare_photo(source, output, 1_000_000)
                actual = cv2.imread(str(output))
                self.assertEqual(actual.shape, (720, 1280, 3))
                self.assertEqual((info["oriented_width"], info["oriented_height"]), (40, 80) if orientation >= 5 else (80, 40))
                # Pillow의 EXIF 처리 결과를 독립적인 비교 기준으로 사용한다.
                from PIL import ImageOps
                with Image.open(source) as original:
                    expected = np.array(ImageOps.exif_transpose(original).convert("RGB"))[:, :, ::-1]
                # 여백을 제외한 사진 영역만 축소하여 방향 보정 결과를 비교한다.
                mask = np.any(actual > 0, axis=2)
                ys, xs = np.where(mask)
                region = actual[ys.min():ys.max()+1, xs.min():xs.max()+1]
                region = cv2.resize(region, (expected.shape[1], expected.shape[0]))
                self.assertLess(float(np.abs(region.astype(float)-expected).mean()), 5)

    def test_rejects_corrupt_missing_too_small_and_oversized_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, out = root / "input.png", root / "output.png"
            for data in (b"not an image", b""):
                source.write_bytes(data)
                with self.assertRaises(VideoError):
                    prepare_photo(source, out, 100)
            Image.new("RGB", (16, 16)).save(source)
            with self.assertRaises(VideoError):
                prepare_photo(source, out, 10000)
            Image.new("RGB", (32, 32)).save(source)
            with self.assertRaises(VideoError):
                prepare_photo(source, out, 1)
            with self.assertRaises(VideoError):
                prepare_photo(root / "missing", out, 100)
