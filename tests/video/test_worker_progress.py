import asyncio
import json

import httpx

from ai_service.services.video.gpu_power import GpuPower, PowerSettings
from scripts.video.worker import stage_from_reports


def write_report(folder, *stages):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'report.json').write_text(json.dumps({'events': [{'stage': s} for s in stages]}))


def test_stage_follows_pipeline_events(tmp_path):
    assert stage_from_reports(tmp_path) == 'ANALYZING_PHOTOS'
    write_report(tmp_path / 'a', 'planning', 'planned')
    assert stage_from_reports(tmp_path) == 'ANALYZING_PHOTOS'
    write_report(tmp_path / 'a', 'planning', 'generating_scenes', 'scene_accepted')
    assert stage_from_reports(tmp_path) == 'GENERATING_VIDEO'
    (tmp_path / 'a' / 'film').mkdir()
    assert stage_from_reports(tmp_path) == 'EDITING_VIDEO'


def test_segment_pipeline_encoding_is_editing(tmp_path):
    write_report(tmp_path / 'b', 'validating', 'generating', 'comfyui_running', 'encoding')
    assert stage_from_reports(tmp_path) == 'EDITING_VIDEO'


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def power(calls, clock, status=200):
    def handle(request):
        calls.append((request.url.path, request.headers['authorization']))
        return httpx.Response(status)
    return GpuPower(PowerSettings('abc123', 'key', idle_stop_seconds=600, start_retry_seconds=300),
                    transport=httpx.MockTransport(handle), clock=clock)


def test_gpu_start_is_rate_limited_and_stop_waits_for_idle():
    calls, clock = [], Clock()
    gpu = power(calls, clock)
    assert asyncio.run(gpu.ensure_started())
    assert not asyncio.run(gpu.ensure_started())  # still inside the retry window
    clock.now = 301
    assert asyncio.run(gpu.ensure_started())
    clock.now = 600
    assert not asyncio.run(gpu.stop_if_idle())  # busy 299 seconds ago
    clock.now = 902
    assert asyncio.run(gpu.stop_if_idle())
    clock.now = 2000
    assert not asyncio.run(gpu.stop_if_idle())  # already stopped
    assert [path for path, _ in calls] == ['/v1/pods/abc123/start'] * 2 + ['/v1/pods/abc123/stop']
    assert calls[0][1] == 'Bearer key'


def test_gpu_start_failure_keeps_jobs_waiting():
    calls, clock = [], Clock()
    assert not asyncio.run(power(calls, clock, status=500).ensure_started())


def test_autostart_is_off_by_default(monkeypatch):
    monkeypatch.delenv('VIDEO_GPU_AUTOSTART', raising=False)
    assert PowerSettings.from_env() is None


def test_gpu_without_models_is_not_ready():
    from ai_service.services.video.config import VideoSettings
    from scripts.video.worker import missing_models, required_models
    required = required_models(VideoSettings())
    empty = {node: {'input': {'required': {field: [[], {}]}}} for node, field, _ in required}
    assert len(missing_models(empty, required)) == len(required) == 6
    full = {}
    for node, field, filename in required:
        full.setdefault(node, {'input': {'required': {field: [[], {}]}}})['input']['required'][field][0].append(filename)
    assert missing_models(full, required) == []
