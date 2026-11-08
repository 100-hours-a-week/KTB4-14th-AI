"""Generated-take screening rejects failures without filling a film by looping."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import httpx
import numpy as np

from ai_service.core.config import Settings
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.quality import (
    analyze_video, choose_take, rank_take_windows, review_take_semantics,
    select_reviewed_take,
)


def patterned(phase=0):
    y, x = np.mgrid[:180, :320]
    checker = (((x + phase) // 12 + y // 12) % 2 * 150 + 45).astype(np.uint8)
    return np.stack([checker, np.roll(checker, 5, axis=0), checker], axis=2)


def write_clip(path, frames):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 24, (320, 180))
    if not writer.isOpened():
        raise RuntimeError('MJPG encoder unavailable')
    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.release()


class TechnicalQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_moving_contiguous_take_has_exact_frames_and_avoids_guide_edges(self):
        path = self.folder / 'moving.avi'
        write_clip(path, [patterned(index * 2) for index in range(120)])
        analysis = analyze_video(path)
        selected = choose_take(analysis, duration_seconds=2)
        self.assertTrue(selected['accepted'])
        self.assertEqual((analysis['fps'], analysis['frame_count']), (24, 120))
        self.assertEqual(selected['frame_count'], 48)
        self.assertGreaterEqual(selected['start_frame'], 6)
        self.assertLessEqual(selected['start_frame'] + 48, 114)
        self.assertEqual(selected['review_status'], 'technical_only_semantic_review_required')
        ranked = rank_take_windows(analysis, duration_seconds=1)
        for a, b in zip(sorted(ranked['candidates'], key=lambda c: c['start_frame']),
                        sorted(ranked['candidates'], key=lambda c: c['start_frame'])[1:]):
            self.assertLessEqual(a['start_frame'] + a['frame_count'], b['start_frame'])

    def test_constant_photo_and_black_video_fail_instead_of_looping(self):
        for label, frame, reason in [('frozen', patterned(), 'frozen_or_nearly_static'),
                                      ('black', np.zeros((180, 320, 3), np.uint8), 'black_frames')]:
            with self.subTest(label=label):
                path = self.folder / f'{label}.avi'
                write_clip(path, [frame] * 72)
                result = choose_take(analyze_video(path), duration_seconds=2)
                self.assertFalse(result['accepted'])
                self.assertIn(reason, result['reasons'])
                self.assertEqual(result['candidates'], [])

    def test_persistently_blurred_moving_subject_is_not_saved_by_motion(self):
        path = self.folder / 'blur.avi'
        write_clip(path, [cv2.GaussianBlur(patterned(index * 2), (51, 51), 15) for index in range(96)])
        result = choose_take(analyze_video(path), duration_seconds=2)
        self.assertFalse(result['accepted'])
        self.assertIn('persistently_blurry', result['reasons'])

    def test_good_motion_window_is_selected_after_bad_prefix(self):
        path = self.folder / 'recover.avi'
        write_clip(path, [np.zeros((180, 320, 3), np.uint8)] * 48 +
                   [patterned()] * 48 + [patterned(index * 2) for index in range(144)])
        result = choose_take(analyze_video(path), duration_seconds=2)
        self.assertTrue(result['accepted'])
        self.assertGreaterEqual(result['start_frame'], 90)
        blocked = choose_take(analyze_video(path), duration_seconds=2,
                              excluded_ranges=[(90, 240)])
        self.assertFalse(blocked['accepted'])

    def test_cannot_retime_or_extend_short_native_source(self):
        path = self.folder / 'short.avi'
        write_clip(path, [patterned(index * 2) for index in range(48)])
        analysis = analyze_video(path)
        self.assertEqual(choose_take(analysis, duration_seconds=3)['reasons'], ['source_shorter_than_take'])
        self.assertEqual(choose_take(analysis, duration_seconds=1, fps=30)['reasons'], ['native_fps_mismatch'])


class SemanticQualityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.video = self.folder / 'moving.avi'
        write_clip(self.video, [patterned(index * 2) for index in range(120)])
        self.reference = self.folder / 'reference.png'
        cv2.imwrite(str(self.reference), patterned())

    async def review(self, answer):
        def handler(request):
            body = json.loads(request.content)
            content = body['messages'][1]['content']
            self.assertEqual(sum(row['type'] == 'image_url' for row in content), 7)
            self.assertIn('bokeh', body['messages'][0]['content'])
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with patch('ai_service.services.video.quality.Settings.from_env', return_value=Settings(openai_api_key='test', openai_model='test')):
                return await review_take_semantics(http, VideoSettings(scene_model_review=True), self.video, [self.reference],
                    start_frame=24, frame_count=48, visible_scene='An evidenced place.', subject_action='Meaningful world motion.')

    async def test_major_geometry_issue_overrides_models_generic_approval(self):
        result = await self.review({'accepted': True, 'confidence': 0.95,
            'rationale': 'The clip has an action, but the grasp changes.',
            'issues': [{'code': 'GEOMETRY_WARP', 'severity': 'major', 'reason': 'A hand merges into the glass.'}]})
        self.assertFalse(result['accepted'])
        self.assertIn('GEOMETRY_WARP', result['reasons'])
        self.assertTrue(result['retryable_generation'])

    async def test_clear_action_with_minor_passing_motion_blur_can_pass(self):
        result = await self.review({'accepted': True, 'confidence': 0.9,
            'rationale': 'The primary subject stays readable while leaves pass.',
            'issues': [{'code': 'BLUR_OR_LOW_DETAIL', 'severity': 'minor', 'reason': 'Passing foreground leaves blur naturally.'}]})
        self.assertTrue(result['accepted'])
        self.assertEqual(result['reasons'], [])

    async def test_missing_review_key_never_falls_back_to_unchecked_take(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.fail('must not call provider without a key'))) as http:
            with patch('ai_service.services.video.quality.Settings.from_env', return_value=Settings()):
                result = await select_reviewed_take(http, VideoSettings(scene_model_review=True), self.video, [self.reference],
                    duration_seconds=2, visible_scene='A place.', subject_action='An action.')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['review_status'], 'unavailable')
        self.assertFalse(result['retryable_generation'])
        self.assertNotIn('start_frame', result)


if __name__ == '__main__':
    unittest.main()


def test_model_review_is_off_by_default_and_skips_openai():
    import asyncio
    from ai_service.services.video.config import VideoSettings
    from ai_service.services.video.quality import review_people_fidelity
    settings = VideoSettings()
    assert settings.scene_model_review is False
    result = asyncio.run(review_people_fidelity(None, settings, Path('x.mp4'), [], start_frame=0, frame_count=24))
    assert result['accepted'] is True and result['review_status'] == 'not_reviewed'
