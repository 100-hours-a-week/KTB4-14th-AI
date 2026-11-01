import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import cv2

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.editing import FilmTake
from ai_service.services.video.media import Media
from ai_service.services.video.premium_render import render_premium_film


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class PremiumRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_ranges_repeated_source_captions_and_exact_audio_delivery(self):
        """Exercise the real filter graph: reuse, crop, Korean overlays and AAC."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = []
            for color in ('blue', 'red'):
                path = root / f'{color}.mp4'
                subprocess.run([
                    'ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi',
                    '-i', f'color={color}:size=320x180:rate=24', '-t', '2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path),
                ], check=True)
                sources.append(path)
            music = root / 'music.wav'
            subprocess.run([
                'ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi',
                '-i', 'sine=frequency=440:sample_rate=48000:duration=3', str(music),
            ], check=True)
            takes = [FilmTake(sources[0], 12, 24, 'opening'),
                     FilmTake(sources[1], 0, 24, 'middle', crop=(32, 18, 256, 144)),
                     FilmTake(sources[0], 0, 24, 'return')]
            result = await render_premium_film(
                Media(VideoSettings()), takes, root / 'render', music,
                output_size=(320, 180), target_seconds=3,
                opening_window=(0.1, 0.8), closing_window=(2.1, 2.8),
            )
            cap = cv2.VideoCapture(result['output_file'])
            self.assertEqual(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 72)
            frames = {}
            for index in (0, 12, 23, 24, 47, 48, 60, 71):
                cap.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = cap.read()
                self.assertTrue(ok)
                frames[index] = frame
            cap.release()
            # Opening stays visible, repeated source returns at the exact cut.
            for index in (0, 23, 48, 71):
                self.assertGreater(float(frames[index][:, :, 0].mean()), 180)
                self.assertLess(float(frames[index][:, :, 2].mean()), 25)
            for index in (24, 47):
                self.assertGreater(float(frames[index][:, :, 2].mean()), 180)
                self.assertLess(float(frames[index][:, :, 0].mean()), 25)
            # Both Korean captions actually reach the encoded frames.
            self.assertGreater(int((frames[12][:, :, 1] > 100).sum()), 80)
            self.assertGreater(int((frames[60][:, :, 1] > 100).sum()), 80)
            media = result['media']
            self.assertAlmostEqual(media['duration_seconds'], 3, places=2)
            self.assertEqual((media['width'], media['height']), (320, 180))
            self.assertEqual(media['audio_codec'], 'aac')
            self.assertEqual(media['audio_channels'], 2)
            streams = json.loads(subprocess.check_output([
                'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', result['output_file'],
            ]))['streams']
            audio = next(stream for stream in streams if stream['codec_type'] == 'audio')
            self.assertEqual(int(audio['sample_rate']), 48000)
            self.assertAlmostEqual(float(audio['duration']), 3, places=2)
            self.assertFalse(result['retiming_applied'])
            self.assertFalse(result['black_opening_added'])
            self.assertEqual(len(result['captions']), 2)
