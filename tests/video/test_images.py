"""사진 형식·크기 검증과 EXIF 회전·반전 및 720p 전처리 결과를 검사한다."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import cv2
    import numpy as np
    from PIL import Image
    from ai_service.services.video.images import assess_quality, prepare_photo
    VIDEO_EXTRAS = True
except ImportError:
    VIDEO_EXTRAS = False

from ai_service.services.video.errors import VideoError


@unittest.skipUnless(VIDEO_EXTRAS, "install uv sync --extra video --extra dev")
class PhotoTests(unittest.TestCase):
    def test_panorama_background_does_not_allocate_oversized_intermediate_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for size in ((4096, 32), (32, 4096)):
                source = root / 'panorama.png'
                Image.new('RGB', size, 'red').save(source)
                with patch('ai_service.services.video.images.cv2.resize', wraps=cv2.resize) as resize:
                    prepare_photo(source, root / 'prepared.png', 1_000_000, fit='blurred_background')
                self.assertTrue(all(call.args[1][0] * call.args[1][1] <= 1280 * 720
                                    for call in resize.call_args_list))

    def test_quality_separates_sharp_blurred_and_bad_exposure(self):
        rng = np.random.default_rng(42)
        sharp = rng.integers(0, 256, (720, 1024, 3), dtype=np.uint8)
        blurred = cv2.GaussianBlur(sharp, (0, 0), 8)
        a, b = assess_quality(sharp), assess_quality(blurred)
        self.assertGreater(a['laplacian_variance'], b['laplacian_variance'] * 10)
        self.assertEqual(a['sharpness'], 'SHARP')
        self.assertEqual(b['sharpness'], 'BLURRY')
        self.assertEqual(assess_quality(np.zeros_like(sharp))['exposure'], 'UNDEREXPOSED')
        self.assertEqual(assess_quality(np.full_like(sharp, 255))['exposure'], 'OVEREXPOSED')

    def test_quality_uses_original_before_blurred_background_and_preserves_subject(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rng = np.random.default_rng(42)
            pixels = rng.integers(30, 220, (800, 400, 3), dtype=np.uint8)
            source = root / 'portrait.png'
            cv2.imwrite(str(source), pixels)
            a = prepare_photo(source, root / 'letterbox.png', 10_000_000)
            b = prepare_photo(source, root / 'filled.png', 10_000_000, fit='blurred_background')
            self.assertEqual(a['quality'], b['quality'])
            self.assertEqual(b['fit'], 'blurred_background')
            filled, letterbox = cv2.imread(str(root / 'filled.png')), cv2.imread(str(root / 'letterbox.png'))
            self.assertGreater(float(filled[:, :100].mean()), 20)
            np.testing.assert_array_equal(filled[:, 460:820], letterbox[:, 460:820])

    def test_iphone_mpo_jpeg_uses_primary_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'iphone.jpg', root / 'prepared.png'
            primary = Image.new('RGB', (80, 40), 'red')
            primary.save(source, format='MPO', save_all=True,
                         append_images=[Image.new('RGB', (80, 40), 'blue')])
            with Image.open(source) as image:
                self.assertEqual((image.format, image.n_frames), ('MPO', 2))
            info = prepare_photo(source, output, 1_000_000)
            self.assertEqual((info['original_width'], info['original_height']), (80, 40))
            pixels = cv2.imread(str(output))
            self.assertGreater(int(pixels[360, 640, 2]), 240)
            self.assertLess(int(pixels[360, 640, 0]), 10)

    def test_cinematic_crop_fills_frame_and_scores_uncropped_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'portrait.png'
            image = Image.new('RGB', (400, 800), 'red')
            image.paste('green', (0, 288, 400, 512))
            image.save(source)
            cropped = prepare_photo(source, root / 'crop.png', 1_000_000, fit='cinematic_crop')
            full = prepare_photo(source, root / 'full.png', 1_000_000)
            self.assertEqual(cropped['quality'], full['quality'])
            self.assertEqual(cropped['visual_signature'], full['visual_signature'])
            pixels = cv2.imread(str(root / 'crop.png'))
            self.assertEqual(pixels.shape, (720, 1280, 3))
            # 중앙의 초록 영역이 화면 양끝까지 차고 배경/검은 여백은 없어야 한다.
            self.assertGreater(float(pixels[360, :, 1].min()), 120)
            self.assertLess(float(pixels[360, :, 2].max()), 5)

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
