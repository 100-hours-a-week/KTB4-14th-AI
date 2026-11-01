"""영상 CLI가 필요한 진단을 출력하면서 비밀정보는 숨기는지 검사한다."""
from contextlib import redirect_stderr
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from ai_service.services.video.errors import VideoError
from scripts.video.run import main, summarize


class CLITests(unittest.TestCase):
    def test_summary_keeps_standard_fast_and_mock_measurements_separate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (mode, profile, seconds) in enumerate([
                ('live', 'standard', 1200), ('live', 'fast', 200), ('mock', 'fast', 1),
            ]):
                folder = root / str(index)
                folder.mkdir()
                report = {'execution_mode': mode, 'generation_profile': profile,
                          'test_kind': 'single_clip_smoke', 'target_duration_seconds': 5,
                          'status': 'completed' if mode == 'live' else 'mock_completed',
                          'total_seconds': seconds}
                (folder / 'report.json').write_text(json.dumps(report))
            groups = summarize(root)['comparisons']
            self.assertEqual(len(groups), 3)
            by_profile = {(g['execution_mode'], g['generation_profile']): g for g in groups}
            self.assertEqual(by_profile[('live', 'fast')]['median_total_seconds'], 200)
            self.assertEqual(by_profile[('live', 'standard')]['median_total_seconds'], 1200)

    def test_preflight_failure_shows_model_diagnostics_without_private_detail(self):
        error = VideoError('comfyui_value_unavailable', node='CLIPLoader', field='clip_name',
                           models=[{'model': 'encoder.safetensors', 'available': False}],
                           url='private-gateway-url', token='private-token')
        output = io.StringIO()
        with patch('sys.argv', ['video', 'check']), \
             patch('scripts.video.run.execute', AsyncMock(side_effect=error)), \
             redirect_stderr(output), self.assertRaises(SystemExit) as stopped:
            main()
        self.assertEqual(stopped.exception.code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result['reason'], 'comfyui_value_unavailable')
        self.assertEqual(result['detail']['node'], 'CLIPLoader')
        self.assertFalse(result['detail']['models'][0]['available'])
        self.assertNotIn('private', output.getvalue())
