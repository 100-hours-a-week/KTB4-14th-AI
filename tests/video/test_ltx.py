"""LTX 단일 요청, 여러 키프레임, 최대 길이와 음악 처리의 계약을 검사한다."""
from pathlib import Path
import shutil
import tempfile
import unittest

import cv2
import httpx
import numpy as np
from pydantic import ValidationError

from ai_service.schemas.video import MemoryVideoRequest,TravelStoryboard,TravelShot
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.ltx import build_ltx_workflow,keyframe_plan,single_video_prompt
from ai_service.services.video.pipeline import VideoPipeline
from ai_service.services.video.workflow import validate_workflow,model_availability
from ai_service.services.video.errors import VideoError
from scripts.video.mock_comfyui import MockComfyUI,object_info


def story(indices):
    return TravelStoryboard(narrative='A returning traveler explores the real places with visible physical motion.',
        shots=[TravelShot(source_index=i,visible_scene='A real outdoor place matching the reference.',
                         subject_action='Wind moves the trees and water flows naturally.',
                         camera_path='Walk forward with three-dimensional foreground parallax.') for i in indices])


class LTXGraphTests(unittest.TestCase):
    def test_distilled_flf_sampler_uses_explicit_zero_eta_without_cfg_pp(self):
        graph=build_ltx_workflow(VideoSettings(),images=['a.png','b.png'],duration=30,
                                 prompt='A continuous coastal camera move.',seed=0,prefix='test')
        validate_workflow(graph,object_info())
        self.assertEqual(graph['15'],{'class_type':'SamplerEulerAncestral',
                                     'inputs':{'eta':0.0,'s_noise':1.0}})
        self.assertEqual(graph['17']['inputs']['sampler'],['15',0])
        self.assertEqual(graph['13']['inputs']['cfg'],1.0)
        self.assertNotIn('KSamplerSelect',[node['class_type'] for node in graph.values()])
        unavailable=object_info();unavailable.pop('SamplerEulerAncestral')
        with self.assertRaises(VideoError) as stopped:validate_workflow(graph,unavailable)
        self.assertEqual(stopped.exception.reason,'comfyui_node_missing')

    def test_v3_combo_model_list_is_checked_before_gpu_submission(self):
        settings=VideoSettings()
        graph=build_ltx_workflow(settings,images=['a.png','b.png'],duration=4,prompt='Korean setting',seed=0,prefix='test')
        info=object_info()
        encoder=info['LTXAVTextEncoderLoader']['input']['required']
        encoder['text_encoder']=['COMBO',{'options':[settings.ltx_text_encoder]}]
        validate_workflow(graph,info)
        encoder['text_encoder']=['COMBO',{'options':[]}]
        self.assertFalse(next(row for row in model_availability(graph,info) if row['field']=='text_encoder')['available'])
        with self.assertRaises(VideoError) as stopped:validate_workflow(graph,info)
        self.assertEqual(stopped.exception.reason,'comfyui_value_unavailable')

    def test_six_photos_condition_one_twenty_second_sampler_with_cropped_guides(self):
        settings=VideoSettings()
        request=MemoryVideoRequest(photos=[f'{i}.png' for i in range(6)])
        plan=keyframe_plan(6,20)
        self.assertEqual(plan['keyframe_indices'],[0,96,192,288,384,480])
        self.assertEqual(plan['frames'],481)
        graph=build_ltx_workflow(settings,images=[str(p) for p in request.photos],duration=20,
                                 prompt=single_video_prompt(request),seed=0,prefix='test')
        validate_workflow(graph,object_info())
        kinds=[node['class_type'] for node in graph.values()]
        self.assertEqual(kinds.count('SamplerCustomAdvanced'),1)
        self.assertEqual(kinds.count('LTXVAddGuide'),6)
        self.assertEqual(kinds.count('SaveVideo'),1)
        self.assertNotIn('WanFirstLastFrameToVideo',kinds)
        self.assertEqual(graph['20']['inputs']['samples'],['19',2])
        self.assertIn('South Korea',graph['3']['inputs']['text'])
        self.assertIn('instrumental',graph['3']['inputs']['text'])
        for kwargs in ({'max_duration_seconds':31},{'max_selected_photos':13}):
            with self.assertRaises(ValidationError):MemoryVideoRequest(photos=['a','b'],backend='ltx23',**kwargs)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class LTXPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_paired_scenes_to_one_thirty_second_request_with_native_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);photos=[];rng=np.random.default_rng(453)
            for index in range(8):
                image=rng.integers(0,256,(720,1280,3),dtype=np.uint8)
                path=root/f'{index}.jpg';cv2.imwrite(str(path),image);photos.append(path)
            settings=VideoSettings(workspace=root/'jobs',comfyui_url='http://mock',poll_interval=0.001)
            mock=MockComfyUI()
            paired=story(range(0,8,2))
            paired.title='Our journey';paired.closing_line='Until next time'
            paired.emotional_arc='An intimate opening, warm highlight, and a gentle resolving ending.'
            for shot,seconds in zip(paired.shots,[3,6,9,12]):
                shot.end_source_index=shot.source_index+1;shot.duration_seconds=seconds
            async with httpx.AsyncClient(transport=httpx.MockTransport(mock.handle)) as http:
                result=await VideoPipeline(http,settings,execution_mode='mock').generate(MemoryVideoRequest(
                    photos=photos,storyboard=paired,backend='ltx23'))
            self.assertEqual(result['backend'],'ltx23')
            self.assertEqual(result['status'],'mock_completed')
            self.assertEqual(result['selected_photo_count'],8)
            self.assertEqual(result['target_duration_seconds'],30)
            self.assertEqual(len(mock.prompts),1)
            self.assertEqual(len(mock.uploads),8)
            self.assertEqual(result['clips'][0]['keyframe_indices'],[0,71,72,215,216,431,432,720])
            self.assertEqual(result['memory_presentation']['title'],'Our journey')
            self.assertTrue((Path(result['output_file']).parent/'opening-title.png').is_file())
            self.assertFalse(result['editing']['applied'])
            self.assertEqual(result['visual_quality_status'],'pending_revisit_motion_review')
            self.assertEqual(len(result['clips']),1)
            self.assertEqual(result['clips'][0]['kind'],'single_keyframe_video')
            self.assertEqual(result['music']['source'],'MOCK_GENERATED_AUDIO')
            self.assertFalse(result['gpu_inference_executed'])
            self.assertEqual(result['media']['duration_seconds'],30)
            self.assertEqual(result['media']['fps'],24)
            self.assertEqual(result['media']['audio_channels'],2)
            import subprocess
            signal=np.frombuffer(subprocess.check_output(['ffmpeg','-v','error','-i',result['output_file'],
                '-map','0:a:0','-ac','1','-ar','8000','-f','f32le','pipe:1']),dtype='<f4')
            opening=float(np.sqrt(np.mean(signal[8000:16000]**2)))
            highlight=float(np.sqrt(np.mean(signal[160000:168000]**2)))
            self.assertGreater(highlight,opening*1.4)
            self.assertIsNone(result['estimated_request_window_cost_usd'])
