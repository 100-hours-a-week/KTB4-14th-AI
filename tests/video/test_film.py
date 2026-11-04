import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import cv2

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.editing import FilmTake
from ai_service.services.video.errors import VideoError
from ai_service.services.video.media import Media


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class FilmEditingTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_ranges_make_exact_hard_cuts_without_speed_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);sources=[]
            for color in ['blue','red']:
                path=root/f'{color}.mp4'
                subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color={color}:size=128x72:rate=24',
                    '-t','2','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True)
                sources.append(path)
            media=Media(VideoSettings())
            takes=[FilmTake(sources[0],12,24,'opening'),FilmTake(sources[1],0,48,'highlight',crop=(16,8,96,54))]
            result=await media.assemble_memory_film(takes,root/'film',duration_seconds=3)
            self.assertAlmostEqual(result['media']['duration_seconds'],3,places=2)
            cap=cv2.VideoCapture(result['output_file'])
            cap.set(cv2.CAP_PROP_POS_FRAMES,23);ok,before=cap.read();self.assertTrue(ok)
            cap.set(cv2.CAP_PROP_POS_FRAMES,24);ok,after=cap.read();self.assertTrue(ok);cap.release()
            self.assertGreater(float(before[:,:,0].mean()),float(before[:,:,2].mean())+150)
            self.assertGreater(float(after[:,:,2].mean()),float(after[:,:,0].mean())+150)
            with self.assertRaises(VideoError):await media.assemble_memory_film(takes,root/'bad',duration_seconds=4)
