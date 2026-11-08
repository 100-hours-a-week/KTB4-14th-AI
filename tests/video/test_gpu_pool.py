"""Serverless GPU pool: many video jobs at once, one or two GPU endpoints each."""
import asyncio
from types import SimpleNamespace

import pytest

from ai_service.api.routes import video_jobs
from scripts.video import worker


def test_two_gpus_per_job_until_more_people_wait(monkeypatch):
    monkeypatch.setattr(worker, 'MAX_GPUS_PER_JOB', 2)
    assert worker.gpu_allocation(10, 1) == 2
    assert worker.gpu_allocation(2, 1) == 2
    assert worker.gpu_allocation(10, 10) == 1
    assert worker.gpu_allocation(1, 1) == 1
    assert worker.gpu_allocation(0, 3) == 0
    assert worker.gpu_allocation(5, 0) == 0


def simulate(people):
    """GPUs each job gets when `people` jobs are queued at the same moment on ten endpoints."""
    free, given = 10, []
    for waiting in range(people, 0, -1):
        count = worker.gpu_allocation(free, waiting)
        if not count:
            break
        given.append(count)
        free -= count
    return given


def test_one_gpu_per_job_by_default():
    assert worker.MAX_GPUS_PER_JOB == 1
    assert worker.gpu_allocation(10, 1) == 1
    assert simulate(5) == [1] * 5


def test_five_people_get_two_gpus_each_and_ten_people_one_each(monkeypatch):
    monkeypatch.setattr(worker, 'MAX_GPUS_PER_JOB', 2)
    assert simulate(5) == [2, 2, 2, 2, 2]
    assert simulate(10) == [1] * 10
    assert simulate(3) == [2, 2, 2]
    assert len(simulate(12)) == 10  # the other two wait for a free GPU


def test_child_job_is_pinned_to_its_own_endpoints(monkeypatch):
    monkeypatch.setenv('COMFYUI_EXTRA_URLS', 'https://other.api.runpod.ai')
    env = worker.job_env(['https://a.api.runpod.ai', 'https://b.api.runpod.ai'])
    assert (env['COMFYUI_URL'], env['COMFYUI_EXTRA_URLS']) == ('https://a.api.runpod.ai', 'https://b.api.runpod.ai')
    assert worker.job_env(['https://a.api.runpod.ai'])['COMFYUI_EXTRA_URLS'] == ''


def test_serve_parallel_runs_jobs_at_once_and_reuses_released_gpus(monkeypatch):
    monkeypatch.setattr(worker, 'MAX_GPUS_PER_JOB', 2)
    urls = [f'https://e{i}.api.runpod.ai' for i in range(4)]
    base = SimpleNamespace(comfyui_url=urls[0], comfyui_extra_urls=tuple(urls[1:]))
    queue = ['j1', 'j2', 'j3']
    started, states, gate = [], [], {}

    class Lease:
        def execute(self, sql, *args):
            return SimpleNamespace(fetchall=lambda: [{'id': job} for job in queue])

    def claim(lease, job_id, assigned=None):
        queue.remove(job_id)
        return {'id': job_id, 'assigned': assigned}

    async def run_on_gpus(lease, base, row, chosen):
        started.append((row['id'], tuple(chosen), row['assigned']))
        gate[row['id']] = asyncio.Event()
        await gate[row['id']].wait()

    monkeypatch.setattr(worker, 'claim', claim)
    monkeypatch.setattr(worker, 'run_on_gpus', run_on_gpus)
    monkeypatch.setattr(worker, 'heartbeat', lambda lease, state: states.append(state))
    async def allow(settings):
        pass
    monkeypatch.setattr(worker, 'allow_one_worker', allow)
    monkeypatch.setattr(worker, 'gpu_endpoints', lambda settings: [])

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(worker.asyncio, 'sleep', lambda seconds: real_sleep(0))
        server = asyncio.create_task(worker.serve_parallel(Lease(), base))
        for _ in range(20):
            await real_sleep(0)
        # 3 jobs, 4 GPUs: the first gets two GPUs, the next two one each, all at the same time.
        assert [count for _, chosen, _ in started for count in [len(chosen)]] == [2, 1, 1]
        assert started[0][2] == '서버리스 GPU 2대'
        assert len({url for _, chosen, _ in started for url in chosen}) == 4
        queue.append('j4')
        for _ in range(20):
            await real_sleep(0)
        assert len(started) == 3 and states[-1] == 'ALL_GPUS_BUSY'
        gate['j2'].set()
        for _ in range(20):
            await real_sleep(0)
        # j2's GPU is reused at once by the waiting job.
        assert started[3][0] == 'j4' and started[3][1] == started[1][1]
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server

    asyncio.run(scenario())


def test_status_estimate_when_every_gpu_is_busy(monkeypatch):
    monkeypatch.setattr(video_jobs, 'gpu_pool_size', lambda: 10)
    note = video_jobs.estimate_note('QUEUED', 3, 'ALL_GPUS_BUSY')
    assert 'GPU 10대가 모두 사용 중' in note and '앞에 2개' in note
    assert '10분' in video_jobs.estimate_note('RUNNING', None, 'BUSY', '서버리스 GPU 1대')
    assert '앞에' not in video_jobs.estimate_note('QUEUED', 2, 'BUSY')


def test_recycle_drops_the_stuck_worker_then_allows_one_again():
    import httpx
    calls, health = [], iter([{'throttled': 1}, {}])

    def handler(request):
        calls.append((request.method, request.url.path, request.content.decode()))
        if request.method == 'GET':
            return httpx.Response(200, json={'workers': next(health)})
        return httpx.Response(200, json={})

    settings = SimpleNamespace(comfyui_url='https://abc123.api.runpod.ai', runpod_api_key='k')

    async def run():
        real_sleep = asyncio.sleep
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            worker.asyncio.sleep, saved = (lambda s: real_sleep(0)), worker.asyncio.sleep
            try:
                await worker.recycle_endpoint(settings, http=http)
            finally:
                worker.asyncio.sleep = saved

    asyncio.run(run())
    patches = [body for method, _, body in calls if method == 'PATCH']
    assert patches == ['{"workersMax":0}', '{"workersMax":1}']
    assert all(path in ('/v1/endpoints/abc123', '/v2/abc123/health') for _, path, _ in calls)


def test_recycle_always_restores_one_worker_even_if_dropping_fails():
    import httpx
    bodies = []

    def handler(request):
        bodies.append(request.content.decode())
        if request.content.decode() == '{"workersMax":0}':
            return httpx.Response(500)
        return httpx.Response(200, json={})

    settings = SimpleNamespace(comfyui_url='https://abc123.api.runpod.ai', runpod_api_key='k')

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await worker.recycle_endpoint(settings, http=http)

    asyncio.run(run())
    assert bodies[-1] == '{"workersMax":1}'


def test_tracker_recycles_a_gpu_sold_out_for_too_long(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'report.json').write_text('{"events": [{"stage": "planned"}]}')
    recycled, details = [], []
    monkeypatch.setattr(worker, 'SOLD_OUT_RECYCLE_SECONDS', 0)
    monkeypatch.setattr(worker, 'set_detail', lambda job, text: details.append(text))
    monkeypatch.setattr(worker, 'update', lambda *a, **k: None)

    async def sold_out(settings):
        return 'GPU_SOLD_OUT'

    async def recycle(settings):
        recycled.append(settings)

    monkeypatch.setattr(worker, 'serverless_state', sold_out)
    monkeypatch.setattr(worker, 'recycle_endpoint', recycle)
    settings = SimpleNamespace(endpoint_mode='serverless')

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(worker.asyncio, 'sleep', lambda seconds: real_sleep(0))
        task = asyncio.create_task(worker.track_progress('job', tmp_path, 3, settings))
        for _ in range(50):
            await real_sleep(0)
            if recycled:
                break
        task.cancel()

    asyncio.run(scenario())
    assert recycled
    assert any('다른 빈 GPU를 찾는 중' in text for text in details)
