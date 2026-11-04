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


class FakeRunPod:
    """In-memory RunPod REST API: list, start (fails when the machine has no GPU), create, delete."""

    def __init__(self, pods=(), start_fails=False, create_fails=False):
        self.pods = {pod['id']: dict(pod) for pod in pods}
        self.start_fails, self.create_fails, self.calls = start_fails, create_fails, []

    def handle(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        assert request.headers['authorization'] == 'Bearer key'
        if method == 'GET':
            return httpx.Response(200, json=list(self.pods.values()))
        if method == 'POST' and path.endswith('/start'):
            if self.start_fails:
                return httpx.Response(500, text='There are no instances currently available')
            self.pods[path.split('/')[-2]]['desiredStatus'] = 'RUNNING'
            return httpx.Response(200, json={})
        if method == 'POST':
            if self.create_fails:
                return httpx.Response(500, text='There are no instances currently available')
            body = json.loads(request.content)
            assert body['networkVolumeId'] == 'vol1' and body['name'].startswith('audigo-video-gpu-')
            self.pods['new'] = {'id': 'new', 'name': body['name'], 'desiredStatus': 'RUNNING'}
            return httpx.Response(200, json=self.pods['new'])
        if method == 'DELETE':
            self.pods.pop(path.split('/')[-1])
            return httpx.Response(204)
        raise AssertionError(request)


def power(api, clock):
    return GpuPower(PowerSettings('key', 'vol1', idle_stop_seconds=600, start_retry_seconds=120),
                    transport=httpx.MockTransport(api.handle), clock=clock)


OTHER = {'id': 'manual', 'name': 'audigo-video-diversity-v4', 'desiredStatus': 'EXITED'}


def test_creates_a_pod_on_the_volume_when_none_exists_and_leaves_other_pods_alone():
    api, clock = FakeRunPod([OTHER]), Clock()
    gpu = power(api, clock)
    assert asyncio.run(gpu.ensure_started())
    assert ('POST', '/v1/pods') in api.calls and 'manual' in api.pods
    assert not asyncio.run(gpu.ensure_started())  # inside the retry window
    clock.now = 121
    assert not asyncio.run(gpu.ensure_started())  # our Pod is already running (booting)


def test_stopped_pod_that_cannot_restart_is_replaced():
    stuck = {'id': 'old', 'name': 'audigo-video-gpu-1', 'desiredStatus': 'EXITED'}
    api = FakeRunPod([stuck, OTHER], start_fails=True)
    assert asyncio.run(power(api, Clock()).ensure_started())
    assert ('DELETE', '/v1/pods/old') in api.calls and set(api.pods) == {'manual', 'new'}


def test_no_free_gpu_keeps_jobs_waiting():
    api = FakeRunPod(create_fails=True)
    assert not asyncio.run(power(api, Clock()).ensure_started())


def test_idle_pods_are_deleted_only_after_the_idle_period():
    api, clock = FakeRunPod([{'id': 'p1', 'name': 'audigo-video-gpu-1', 'desiredStatus': 'RUNNING'}, OTHER]), Clock()
    gpu = power(api, clock)
    clock.now = 599
    assert not asyncio.run(gpu.stop_if_idle())
    clock.now = 601
    assert asyncio.run(gpu.stop_if_idle())
    assert set(api.pods) == {'manual'}


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


def test_newer_combo_format_lists_models():
    from scripts.video.worker import missing_models
    required = [('LTXAVTextEncoderLoader', 'text_encoder', 'gemma.safetensors')]
    combo = {'LTXAVTextEncoderLoader': {'input': {'required': {'text_encoder': ['COMBO', {'options': ['gemma.safetensors']}]}}}}
    assert missing_models(combo, required) == []
    empty = {'LTXAVTextEncoderLoader': {'input': {'required': {'text_encoder': ['COMBO', {'options': []}]}}}}
    assert missing_models(empty, required) == ['gemma.safetensors']
