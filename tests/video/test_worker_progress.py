import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ai_service.services.video.gpu_power import A100_PCIE, AUTO_GPU, RTX_PRO_6000, GpuPower, PowerSettings
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
        self.create_bodies = []

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
            body = json.loads(request.content)
            self.create_bodies.append(body)
            if self.create_fails:
                return httpx.Response(500, text='There are no instances currently available')
            assert body['networkVolumeId'] == 'vol1' and body['name'].startswith('audigo-video-gpu-')
            assert body['ports'] == ['8189/http', '22/tcp'] and body['env']['COMFYUI_API_TOKEN'] == 'g' * 40
            assert 'gpu_boot.sh' in body['dockerStartCmd'][0] and body['containerDiskInGb'] >= 80
            self.pods['new'] = {'id': 'new', 'name': body['name'], 'desiredStatus': 'RUNNING'}
            return httpx.Response(200, json=self.pods['new'])
        if method == 'DELETE':
            self.pods.pop(path.split('/')[-1])
            return httpx.Response(204)
        raise AssertionError(request)


def power(api, clock):
    return GpuPower(PowerSettings('key', 'vol1', 'g' * 40, idle_stop_seconds=600, start_retry_seconds=120),
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


def test_explicit_gpu_stays_queued_when_sold_out_even_with_another_gpu_running():
    warm = {'id': 'warm', 'name': 'audigo-video-gpu-1', 'desiredStatus': 'RUNNING',
            'gpu': {'id': A100_PCIE, 'count': 1}}
    api = FakeRunPod([warm], create_fails=True)
    gpu = power(api, Clock())
    assert not asyncio.run(gpu.ensure_started(RTX_PRO_6000))
    assert RTX_PRO_6000 in gpu.capacity_waiting
    assert api.create_bodies[0]['gpuTypeIds'] == [RTX_PRO_6000]
    assert set(api.pods) == {'warm'}


def test_auto_prefers_a_warm_approved_gpu_or_asks_runpod_for_available_models():
    warm = {'id': 'warm', 'name': 'audigo-video-gpu-1', 'desiredStatus': 'RUNNING',
            'gpu': {'id': A100_PCIE, 'count': 1}}
    api = FakeRunPod([warm])
    assert not asyncio.run(power(api, Clock()).ensure_started(AUTO_GPU))
    assert not api.create_bodies

    cold = FakeRunPod()
    assert asyncio.run(power(cold, Clock()).ensure_started(AUTO_GPU))
    assert cold.create_bodies[0]['gpuTypeIds'] == [RTX_PRO_6000, A100_PCIE]
    assert cold.create_bodies[0]['gpuTypePriority'] == 'availability'
    assert cold.create_bodies[0]['gpuCount'] == 1


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


@pytest.mark.parametrize('audio_streams', [0, 1])
def test_web_worker_requires_music_before_publishing(tmp_path, monkeypatch, audio_streams):
    from ai_service.services.video.config import VideoSettings
    from ai_service.services.video.errors import VideoError
    from scripts.video import worker

    output = tmp_path / 'final.mp4'
    generate = AsyncMock(return_value={
        'status': 'completed', 'output_file': str(output), 'media': {'audio_streams': audio_streams},
        'music': {'source': 'BUILTIN_PRESET_FALLBACK'},
    })
    publish = AsyncMock(return_value={'video_key': 'jobs/final.mp4'})
    update = Mock()
    monkeypatch.setattr(worker, 'VideoPipeline', Mock(return_value=Mock(generate=generate)))
    monkeypatch.setattr(worker, 'publish_video', publish)
    monkeypatch.setattr(worker, 'update', update)
    payload = {'music': 'NONE', 'duration_seconds': 30, 'memory_context': '', 'memory_style': 'WARM'}
    task = worker.generate_and_publish('job', payload, [tmp_path / 'a.jpg', tmp_path / 'b.jpg'], VideoSettings())
    if audio_streams:
        asyncio.run(task)
        publish.assert_awaited_once_with(output)
        update.assert_called_with('job', 'COMPLETED', status='COMPLETED', publication=publish.return_value,
                                  notice=worker.MUSIC_FALLBACK_NOTICE)
    else:
        with pytest.raises(VideoError) as failure:
            asyncio.run(task)
        assert failure.value.reason == 'final_video_music_missing'
        publish.assert_not_awaited()
        update.assert_not_called()
    generate.assert_awaited_once()
    assert generate.await_args.args[0].music.preset == 'AUTO'


def test_progress_detail_names_the_exact_step(tmp_path):
    import json
    from scripts.video.worker import progress_detail
    assert progress_detail(tmp_path, 10) == '사진을 작업 폴더로 옮기는 중'
    run = tmp_path / 'abc'
    run.mkdir()
    (run / 'photo-0000.png').write_bytes(b'x')
    assert 'OpenCV' in progress_detail(tmp_path, 10) and '1/10장' in progress_detail(tmp_path, 10)
    report = {'events': [{'stage': 'planning'}], 'photo_screening': [{'decision': 'selected'}] * 7}
    (run / 'report.json').write_text(json.dumps(report))
    assert progress_detail(tmp_path, 10) == 'OpenAI가 사진 내용을 보고 장면을 구성하는 중 (통과한 사진 7/10장)'
    report['events'].append({'stage': 'planned'})
    (run / 'report.json').write_text(json.dumps(report))
    (run / 'score' / 'attempt-0').mkdir(parents=True)
    assert progress_detail(tmp_path, 10) == 'GPU에서 배경음악 작곡 중 (1번째 시도)'
    report['events'].append({'stage': 'scene_rejected'})
    report.update(planned_scene_count=6, clips=[{'status': 'accepted'}, {'status': 'rejected'}])
    (run / 'report.json').write_text(json.dumps(report))
    (run / 'scene-01-attempt-01').mkdir()
    assert progress_detail(tmp_path, 10) == 'GPU에서 장면 2/6 (2번째 시도) 만드는 중 · 통과 1개, 탈락 1개'
    (run / 'scene-03-attempt-00').mkdir()
    assert progress_detail(tmp_path, 10) == 'GPU 2대에서 장면 2/6 (2번째 시도), 4/6 (1번째 시도) 만드는 중 · 통과 1개, 탈락 1개'
    (run / 'scene-07-attempt-00').mkdir()
    assert '대체 장면 2 (1번째 시도)' in progress_detail(tmp_path, 10)
