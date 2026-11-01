import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock,patch

from PIL import Image

from ai_service.schemas.video import TravelStoryboard,TravelShot,MemoryVideoRequest
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.ltx import build_ltx_workflow,single_video_prompt,story_durations,keyframe_plan
from ai_service.services.video.storyboard import validate_storyboard,plan_travel_story,scene_candidates


def make_story(indices):
    return TravelStoryboard(narrative='A first-person return to the Korean coast and forest train station.',
        shots=[TravelShot(source_index=i,visible_scene='A real Korean coastal path with grass and visible water.',
            subject_action='Waves break against the rocks while wind bends the grass.',
            camera_path='Walk along the path and turn toward the sea with depth parallax.') for i in indices])


class StoryboardTests(unittest.TestCase):
    def test_memory_beats_cover_full_trip_with_variable_pacing(self):
        s=make_story([0,19,32,41,62,105])
        durations=[3,4,5,5,7,6]
        for shot,end,seconds in zip(s.shots,[1,20,33,43,66,107],durations):
            shot.end_source_index=end;shot.duration_seconds=seconds
        self.assertEqual(story_durations(s,30),durations)
        plan=keyframe_plan(12,30,scene_pairs=True,scene_durations=durations)
        self.assertEqual(plan['keyframe_indices'],[0,71,72,167,168,287,288,407,408,575,576,720])
        self.assertIn('17.0-24.0s',single_video_prompt(MemoryVideoRequest(photos=['a','b'],storyboard=s)))
        s.shots[-1].duration_seconds=5
        with self.assertRaises(VideoError):story_durations(s,30)
        s.shots[-1].duration_seconds=None
        with self.assertRaises(VideoError):story_durations(s,30)

    def test_scenery_only_story_does_not_request_invented_korean_faces(self):
        s=make_story([0,1])
        for shot in s.shots:shot.people_visible=False
        prompt=single_video_prompt(MemoryVideoRequest(photos=['a','b'],storyboard=s))
        self.assertIn('The view stays on the landscape throughout',prompt)
        self.assertNotIn('face should be set as a typical Korean',prompt)
        s.shots[0].people_visible=True
        self.assertIn('existing Korean travelers',single_video_prompt(MemoryVideoRequest(photos=['a','b'],storyboard=s)))
        for forbidden in ['printed photo','photo montage','frozen photo','collage','dissolve']:
            self.assertNotIn(forbidden,prompt.lower())
    def test_scene_pairs_are_ordered_non_overlapping_and_cut_between_adjacent_guides(self):
        from ai_service.services.video.ltx import keyframe_plan
        s=make_story([0,3]);s.shots[0].end_source_index=2;s.shots[1].end_source_index=5
        self.assertEqual(validate_storyboard(s,list(range(6)),4),[0,2,3,5])
        self.assertEqual(keyframe_plan(8,30,scene_pairs=True)['keyframe_indices'],[0,179,180,359,360,539,540,720])
        self.assertIn('instant hard cut',single_video_prompt(MemoryVideoRequest(photos=['a','b'],storyboard=s)))
        for end in [0,3,99]:
            s.shots[0].end_source_index=end
            with self.assertRaises(VideoError):validate_storyboard(s,list(range(6)),4)

    def test_pair_candidates_include_good_neighbours_but_exclude_rejected_quality(self):
        selection={'selected_source_indices':[2,5], 'photos':[
            {'source_index':i,'decision':'quality_rejected' if i==3 else 'near_duplicate'} for i in range(7)]}
        self.assertEqual(scene_candidates(selection),[0,1,2,4,5,6])

    def test_unknown_repeated_and_out_of_order_references_are_rejected(self):
        for indices in ([0,99],[2,0],[0,0],[0]):
            with self.assertRaises(VideoError):validate_storyboard(make_story(indices),[0,1,2],4)
        self.assertEqual(validate_storyboard(make_story([0,2]),[0,1,2],4),[0,2])

    def test_30_seconds_is_one_motion_directed_generation_and_soft_reference(self):
        request=MemoryVideoRequest(photos=['a','b'],storyboard=make_story([0,1]))
        prompt=single_video_prompt(request)
        graph=build_ltx_workflow(VideoSettings(),images=['a.png','b.png'],duration=30,prompt=prompt,
            seed=0,prefix='test',reference_strength=request.reference_strength)
        self.assertEqual(graph['6']['inputs']['length'],721)
        self.assertEqual(sum(n['class_type']=='SamplerCustomAdvanced' for n in graph.values()),1)
        self.assertEqual(graph['200']['inputs']['strength'],0.7)
        self.assertIn('Waves break',prompt)
        self.assertIn('first-person travel footage in South Korea, 30 seconds',prompt)
        self.assertIn('depth parallax',prompt)


class StoryboardAPIContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_input_and_structured_result_keep_real_source_indices(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'source.jpg';Image.new('RGB',(1200,900),'blue').save(path)
            paired=make_story([1]);paired.shots[0].end_source_index=4
            completion=AsyncMock(return_value=paired.model_dump_json())
            with patch('ai_service.services.video.storyboard.Settings.from_env') as settings, \
                 patch('ai_service.services.video.storyboard.OpenAIClient.complete',completion):
                from ai_service.core.config import Settings
                settings.return_value=Settings(openai_api_key='test-only-key')
                result,meta=await plan_travel_story(None,VideoSettings(),[(1,path),(4,path)],
                    max_photos=4,duration=30,context='대한민국 여행')
            self.assertEqual(validate_storyboard(result,[1,4],4),[1,4])
            messages=completion.call_args.args[0]
            images=[p for p in messages[1]['content'] if p['type']=='image_url']
            self.assertEqual(len(images),2)
            self.assertTrue(images[0]['image_url']['url'].startswith('data:image/jpeg;base64,'))
            self.assertEqual(meta['candidate_count'],2)
