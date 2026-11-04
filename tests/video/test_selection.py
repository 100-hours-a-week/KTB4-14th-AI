"""대표 사진 선별과 FLF 구간 수·최대 길이 및 GPU 전 미리보기를 검증한다."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import cv2
import httpx
import numpy as np
from pydantic import ValidationError

from ai_service.schemas.video import MemoryVideoRequest
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.pipeline import VideoPipeline
from ai_service.services.video.selection import select_photos, ordered_photo_files, near_duplicate
from scripts.video.mock_comfyui import MockComfyUI


def candidate(index, *, sharpness='SHARP', variance=400, signature=None):
    histogram = [0.] * 48
    histogram[index % 48] = 1.
    return {'source_index': index, 'oriented_width': 1280, 'oriented_height': 720,
            'quality': {'sharpness': sharpness, 'laplacian_variance': variance, 'exposure': 'NORMAL',
                        'warnings': [] if sharpness == 'SHARP' else [sharpness]},
            'visual_signature': signature or {'phash': f'{index:016x}', 'color_histogram': histogram}}


class SelectionTests(unittest.TestCase):
    def test_duplicate_requires_both_layout_and_color_similarity(self):
        first=candidate(0)['visual_signature']
        different_color=candidate(1)['visual_signature']
        different_color['phash']=first['phash']
        self.assertFalse(near_duplicate(first,different_color))
        self.assertFalse(near_duplicate(first,{**first,'phash':'ffffffffffffffff'}))
        self.assertTrue(near_duplicate(first,{**first,'phash':'000000000000ffff'}))

    def test_blurry_removed_better_duplicate_kept_and_order_preserved(self):
        first = candidate(0, variance=180)
        duplicate = candidate(2, variance=900, signature=first['visual_signature'])
        result = select_photos([first, candidate(1, sharpness='BLURRY', variance=3), duplicate, candidate(3)])
        self.assertEqual(result['selected_source_indices'], [2, 3])
        self.assertEqual(result['duration_seconds'], 5)
        self.assertEqual(result['photos'][0]['decision'], 'near_duplicate')
        self.assertEqual(result['photos'][1]['decision'], 'quality_rejected')

    def test_temporal_bins_and_duration_cap_do_not_pad_or_repeat_photos(self):
        result = select_photos([candidate(i) for i in range(30)], max_photos=12,
                               transition_seconds=5, max_duration_seconds=15)
        indices = result['selected_source_indices']
        self.assertEqual(len(indices), 4)
        self.assertEqual(result['duration_seconds'], 15)
        self.assertEqual(indices, sorted(set(indices)))
        for bin_index, index in enumerate(indices):
            self.assertGreaterEqual(index, bin_index * 30 // 4)
            self.assertLess(index, (bin_index+1) * 30 // 4)
        maximum = select_photos([candidate(i) for i in range(37)], max_photos=37)
        self.assertEqual(maximum['duration_seconds'], 180)

    def test_insufficient_real_photos_fails_and_request_is_always_flf(self):
        with self.assertRaises(VideoError) as stopped:
            select_photos([candidate(0, sharpness='BLURRY'), candidate(1)])
        self.assertEqual(stopped.exception.reason, 'insufficient_usable_photos')
        for kwargs in ({'story_mode': 'PHOTO_MOTION'}, {'max_duration_seconds': 181}, {'max_selected_photos': 38}):
            with self.assertRaises(ValidationError):
                MemoryVideoRequest(photos=['a','b'], **kwargs)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class AutoPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_opencv_selection_then_only_selected_adjacent_flf_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rng = np.random.default_rng(42)
            pixels = rng.integers(0, 180, (720, 1280, 3), dtype=np.uint8)
            blue, green = pixels.copy(), pixels.copy()
            blue[:,:,0] = 255
            green[:,:,1] = 255
            arrays = [pixels, pixels, cv2.GaussianBlur(pixels,(0,0),12), np.zeros_like(pixels), blue, green]
            photos = []
            for i, image in enumerate(arrays):
                path=root/f'{i}.jpg';cv2.imwrite(str(path), image);photos.append(path)
            broken=root/'broken.jpg';broken.write_bytes(b'broken');photos.append(broken)
            self.assertEqual(len(ordered_photo_files(root)), 7)
            mock=MockComfyUI()
            settings=VideoSettings(workspace=root/'jobs',comfyui_url='http://mock',poll_interval=0.001)
            async with httpx.AsyncClient(transport=httpx.MockTransport(mock.handle)) as http:
                pipeline=VideoPipeline(http,settings,execution_mode='mock')
                request=MemoryVideoRequest(photos=photos,max_selected_photos=3,preview_only=True,backend='wan_segments')
                preview=await pipeline.generate(request)
                self.assertEqual(preview['status'],'selection_completed')
                self.assertEqual(mock.calls,[])
                self.assertEqual(preview['selected_photo_count'],3)
                self.assertEqual(preview['target_duration_seconds'],10)
                self.assertEqual(preview['photo_selection']['invalid_input_count'],1)
                graph=json.loads((settings.workspace/preview['job_id']/'planned-workflow-0000.json').read_text())
                self.assertEqual(graph['81']['class_type'],'WanFirstLastFrameToVideo')
                self.assertNotEqual(graph['80']['inputs']['image'],graph['89']['inputs']['image'])
                self.assertNotIn('output_file',preview)
                result=await pipeline.generate(request.model_copy(update={'preview_only':False}))
            self.assertEqual(result['status'],'mock_completed')
            self.assertEqual(len(mock.prompts),2)
            self.assertEqual(len(mock.uploads),3)
            self.assertEqual(result['photo_selection']['selected_source_indices'],[0,4,5])
            self.assertEqual([(c['start_source_index'],c['end_source_index']) for c in result['clips']],[(0,4),(4,5)])
            self.assertTrue(all(c['kind']=='transition' for c in result['clips']))
            self.assertEqual(result['media']['duration_seconds'],10)
            self.assertEqual(result['media']['audio_channels'],2)
            self.assertEqual(json.loads((Path(result['output_file']).parent/'report.json').read_text()),result)
