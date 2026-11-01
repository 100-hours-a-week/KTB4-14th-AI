"""사용자 음악 선택·음원 출력·추억 연출 조건을 확인한다."""
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np
from pydantic import ValidationError

from ai_service.prompts.video import memory_prompt
from ai_service.schemas.video import ClipRequest, MusicSelection
from ai_service.services.video.music import choose_preset, render_preset
from ai_service.services.video.planning import plan_clips


class MusicTests(unittest.TestCase):
    def test_auto_music_uses_explicit_context_and_selected_style(self):
        auto = MusicSelection()
        self.assertEqual(choose_preset(auto, 'WARM', '친구 생일 축하'), 'BRIGHT')
        self.assertEqual(choose_preset(auto, 'LIVELY', '가족 여행'), 'WARM')
        self.assertEqual(choose_preset(auto, 'LIVELY', ''), 'BRIGHT')
        self.assertEqual(choose_preset(MusicSelection(preset='CALM'), 'LIVELY', '생일'), 'CALM')
        with self.assertRaises(ValidationError):
            MusicSelection(preset='NONE', file=Path('a.mp3'))

    def test_presets_are_audible_distinct_stereo_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            signals = []
            for preset in ('WARM', 'BRIGHT', 'CALM'):
                path = Path(directory) / f'{preset}.wav'
                render_preset(path, preset, 3)
                with wave.open(str(path)) as f:
                    self.assertEqual((f.getnchannels(), f.getframerate()), (2, 22050))
                    self.assertAlmostEqual(f.getnframes() / f.getframerate(), 3)
                    samples = np.frombuffer(f.readframes(f.getnframes()), dtype='<i2').astype(float) / 32768
                self.assertGreater(float(np.sqrt(np.mean(samples ** 2))), 0.01)
                self.assertLessEqual(float(np.abs(samples).max()), 0.73)
                signals.append(samples)
            self.assertFalse(np.array_equal(signals[0], signals[1]))

    def test_prompt_has_memory_style_camera_motion_and_anti_morph_constraints(self):
        request = ClipRequest(photos=['a', 'b'], memory_style='LIVELY', memory_context='친구 여행')
        prompt = memory_prompt(request, plan_clips(2, 5, smoke=True)[0], 0)
        self.assertIn('joyful', prompt)
        self.assertIn('parallax', prompt)
        self.assertIn('Do not dissolve', prompt)
        self.assertIn('친구 여행', prompt)

    def test_photo_motion_distributes_whole_frames_without_wan_frame_padding(self):
        plans = plan_clips(3, 11, story_mode='PHOTO_MOTION')
        self.assertEqual(sum(p.frames for p in plans), 176)
        self.assertEqual(sum(p.duration_seconds for p in plans), 11)
        self.assertEqual([(p.start_index, p.end_index) for p in plans], [(0, 0), (1, 1), (2, 2)])

    def test_korean_context_keeps_original_people_and_does_not_add_neighbors(self):
        request = ClipRequest(photos=['a', 'b'])
        prompt = memory_prompt(request, plan_clips(2, 5, smoke=True)[0], 0)
        self.assertIn('South Korea', prompt)
        self.assertIn('Korean', prompt)
        self.assertIn('original faces', prompt)
        self.assertIn('Keep unoccupied areas empty', prompt)
        self.assertIn('Do not add a person sitting or standing beside', prompt)
