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
