"""Production score failures must not become silent padding or duplicate jobs."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from ai_service.clients.comfyui import ComfyUI
from ai_service.schemas.video import MusicSelection
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.media import Media
from ai_service.services.video.score import (
    ScorePolicy, audio_activity, prepare_production_score, select_score_excerpt,
)


def music_samples(seconds=40, *, end_music=32, tail=5, sample_rate=8000):
    """A test signal with sustained energy, decay, then genuine silence."""
    time = np.arange(round(seconds * sample_rate)) / sample_rate
    envelope = np.ones(len(time))
    after = time >= end_music
    envelope[after] = np.exp(-(time[after] - end_music) * 1.5)
    envelope[time >= end_music + tail] = 0
    signal = (0.2 * np.sin(2 * np.pi * 220 * time)
              + 0.07 * np.sin(2 * np.pi * 331 * time)) * envelope
    return np.column_stack([signal, signal * 0.94]).astype(np.float32)


def write_audio(path, samples, sample_rate=8000):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes((samples * 32767).astype('<i2').tobytes())


class ScoreSelectionTests(unittest.TestCase):
    def test_long_container_with_early_music_end_is_rejected(self):
        signal = music_samples(40, end_music=19, tail=4)
        with self.assertRaises(VideoError) as caught:
            select_score_excerpt(signal, 30, sample_rate=8000)
        self.assertEqual(caught.exception.reason, 'score_composition_ends_early')

    def test_continuous_excerpt_keeps_decay_and_exact_duration(self):
        signal = music_samples()
        result = select_score_excerpt(signal, 30, sample_rate=8000)
        self.assertGreater(result['start_seconds'], 0)
        self.assertEqual(result['end_sample_exclusive'] - result['start_sample'], 30 * 8000)
        self.assertEqual(result['duration_seconds'], 30)
        self.assertGreater(result['ending_energy_drop_db'], 6)
        self.assertGreaterEqual(result['activity']['active_fraction'], .9)
        self.assertLessEqual(result['activity']['trailing_quiet_seconds'], 1.5)
        self.assertIn('no_loop_or_stretch', result['policy'])

    def test_internal_dead_gap_and_flat_truncated_end_fail(self):
        signal = music_samples()
        signal[12 * 8000:14 * 8000] = 0
        with self.assertRaises(VideoError) as caught:
            select_score_excerpt(signal, 30, sample_rate=8000)
        self.assertEqual(caught.exception.reason, 'score_no_usable_continuous_excerpt')
        with self.assertRaises(VideoError):
            select_score_excerpt(music_samples(40, end_music=50), 30, sample_rate=8000)

    def test_silence_and_heavily_clipped_signal_do_not_pass_activity_gate(self):
        with self.assertRaises(VideoError):
            select_score_excerpt(np.zeros((40 * 8000, 2)), 30, sample_rate=8000)
        clipped = music_samples()
        clipped[:8000] = 1
        with self.assertRaises(VideoError) as caught:
            select_score_excerpt(clipped, 30, sample_rate=8000)
        self.assertEqual(caught.exception.reason, 'score_audio_clipped')
        self.assertEqual(audio_activity(np.zeros((8000, 2)), 8000)['last_active_seconds'], None)


class AudioClient:
    """Fake provider transport; real graph construction/FFmpeg decode remain."""
    def __init__(self, sources, *, uncertain=False):
        self.sources = sources
        self.settings = replace(VideoSettings(), poll_interval=.001)
        self.submissions = []
        self.uncertain = uncertain
        self.history_reads = {}

    async def object_info(self):
        return {}

    async def submit(self, graph, client_id):
        self.submissions.append((graph, client_id))
        if self.uncertain:
            raise VideoError('comfyui_submission_uncertain')
        return f'p{len(self.submissions)}'

    async def _json(self, method, route):
        assert method == 'GET'
        prompt_id = route.rsplit('/', 1)[-1]
        self.history_reads[prompt_id] = self.history_reads.get(prompt_id, 0) + 1
        if self.history_reads[prompt_id] == 1:
            return {}
        return {prompt_id: {
            'status': {'completed': True, 'messages': [
                ['execution_start', {'timestamp': 1000}], ['execution_success', {'timestamp': 2000}],
            ]},
            'outputs': {'10': {'audio': [{
                'filename': f'{prompt_id}.flac', 'subfolder': 'score', 'type': 'output',
            }]}},
        }}

    _execution_seconds = staticmethod(ComfyUI._execution_seconds)

    async def download(self, asset, destination):
        index = int(asset['filename'][1:-5]) - 1
        shutil.copyfile(self.sources[index], destination)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class ProductionScoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.media = Media(VideoSettings())
        self.good = self.folder / 'good.wav'
        self.bad = self.folder / 'short-composition.wav'
        write_audio(self.good, music_samples())
        write_audio(self.bad, music_samples(40, end_music=19, tail=4))

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def generate(self, client, folder='job', **kwargs):
        with patch('ai_service.services.video.score.validate_ace_music_workflow'):
            return await prepare_production_score(
                self.media, client, MusicSelection(), self.folder / folder,
                duration_seconds=30, style='LIVELY', seed=17, job_id='test-score', **kwargs,
            )

    async def test_audio_history_and_exact_excerpt_require_one_submission(self):
        client = AudioClient([self.good])
        path, report = await self.generate(client)
        self.assertTrue(path.is_file())
        self.assertEqual(len(client.submissions), 1)
        graph = client.submissions[0][0]
        self.assertEqual(graph['4']['inputs']['duration'], 40)
        self.assertEqual(graph['7']['inputs']['seconds'], 40)
        self.assertEqual(report['source'], 'MODEL_GENERATED')
        self.assertEqual(report['audio']['duration_seconds'], 30)
        self.assertEqual(report['audio']['sample_rate'], 48000)
        self.assertFalse(report['looping_applied'])
        self.assertFalse(report['volume_applied'])
        # A repeated stage returns the same ready result without paying again.
        same, reused = await self.generate(client)
        self.assertEqual(path, same)
        self.assertEqual(report['request_fingerprint'], reused['request_fingerprint'])
        self.assertEqual(len(client.submissions), 1)

    async def test_completed_early_ending_candidate_allows_only_one_replacement(self):
        client = AudioClient([self.bad, self.good])
        path, report = await self.generate(client)
        self.assertTrue(path.is_file())
        self.assertEqual(len(client.submissions), 2)
        self.assertEqual(report['accepted_attempt'], 1)
        self.assertEqual(report['attempts'][0]['reason'], 'score_composition_ends_early')
        self.assertEqual([job[0]['4']['inputs']['seed'] for job in client.submissions], [17, 18])

    async def test_uncertain_submission_never_retries_or_masks_with_fallback(self):
        client = AudioClient([self.good], uncertain=True)
        with self.assertRaises(VideoError) as caught:
            await self.generate(client, fallback_file=self.good)
        self.assertEqual(caught.exception.reason, 'comfyui_submission_uncertain')
        self.assertEqual(len(client.submissions), 1)
        report = json.loads((self.folder / 'job/score/report.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['attempts'][0]['provider_job_may_be_running'])
        self.assertFalse(report['fallback_used'])

    async def test_fallback_requires_explicit_configuration(self):
        client = AudioClient([self.bad])
        path, report = await self.generate(client, policy=ScorePolicy(max_attempts=1), fallback_file=self.good)
        self.assertEqual(report['source'], 'CONFIGURED_FILE_FALLBACK')
        self.assertTrue(report['fallback_used'])
        self.assertEqual(report['audio']['duration_seconds'], 30)
        client = AudioClient([self.bad])
        with self.assertRaises(VideoError) as caught:
            await self.generate(client, folder='no-fallback', policy=ScorePolicy(max_attempts=1))
        self.assertEqual(caught.exception.reason, 'score_quality_failed')

    async def test_two_rejected_scores_use_full_length_preset_without_a_third_submission(self):
        client = AudioClient([self.bad, self.bad])
        path, report = await self.generate(client, allow_preset_fallback=True)

        self.assertTrue(path.is_file())
        self.assertEqual(len(client.submissions), 2)
        self.assertEqual(report['source'], 'BUILTIN_PRESET_FALLBACK')
        self.assertEqual(report['selected_preset'], 'BRIGHT')
        self.assertTrue(report['fallback_used'])
        self.assertEqual(report['audio']['duration_seconds'], 30)
        self.assertGreater(report['activity']['active_fraction'], 0.9)
        self.assertFalse(report['looping_applied'])

        reused, _ = await self.generate(None, allow_preset_fallback=True)
        self.assertEqual(reused, path)

    async def test_explicit_file_none_and_short_file_do_not_call_provider(self):
        user_folder = self.folder / 'user'
        path, report = await prepare_production_score(
            self.media, None, MusicSelection(file=self.good, start_seconds=3, volume=.4),
            user_folder, duration_seconds=30,
        )
        self.assertTrue(path.is_file())
        self.assertEqual(report['source'], 'USER_FILE')
        self.assertEqual(report['volume'], .4)
        self.assertEqual(report['excerpt']['start_seconds'], 3)
        with self.assertRaises(VideoError) as caught:
            await prepare_production_score(
                self.media, None, MusicSelection(file=self.good, start_seconds=15),
                self.folder / 'too-short', duration_seconds=30,
            )
        self.assertEqual(caught.exception.reason, 'score_user_file_too_short')
        path, report = await prepare_production_score(
            self.media, None, MusicSelection(preset='NONE'), self.folder / 'none', duration_seconds=30,
        )
        self.assertIsNone(path)
        self.assertEqual(report['source'], 'NONE')

    async def test_changed_request_cannot_reuse_wrong_duration_audio(self):
        selection = MusicSelection(file=self.good)
        folder = self.folder / 'identity'
        await prepare_production_score(self.media, None, selection, folder, duration_seconds=30)
        with self.assertRaises(VideoError) as caught:
            await prepare_production_score(self.media, None, selection, folder, duration_seconds=20)
        self.assertEqual(caught.exception.reason, 'score_existing_request_mismatch')


if __name__ == '__main__':
    unittest.main()
