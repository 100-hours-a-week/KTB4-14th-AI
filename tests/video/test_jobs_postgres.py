"""Real PostgreSQL contract tests. Set TEST_VIDEO_DATABASE_URL to an isolated test DB."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ai_service.services.video import jobs

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_VIDEO_DATABASE_URL'), reason='Isolated PostgreSQL is required')

@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('VIDEO_DATABASE_URL', os.environ['TEST_VIDEO_DATABASE_URL'])
    monkeypatch.setenv('AUDIGO_API_TOKEN', 'integration-test-token-with-32-characters')
    from ai_service.video_app import create_app
    jobs.initialize()
    with jobs.connection() as conn:
        conn.execute('TRUNCATE video_jobs,video_uploads,video_worker')
        photos = [uuid4(), uuid4()]
        for photo in photos:
            conn.execute('INSERT INTO video_uploads(id,owner_id,storage_key) VALUES(%s,1,%s)', (photo, f'test/{photo}'))
    yield TestClient(create_app()), photos


def headers(owner=1):
    return {'Authorization': 'Bearer integration-test-token-with-32-characters', 'X-Owner-ID': str(owner)}


def payload(photos):
    return {'request_id': str(uuid4()), 'travel_plan_id': 101, 'upload_ids': [str(p) for p in photos]}


def test_duplicate_concurrent_submission_creates_one_job(api):
    client, photos = api
    body = payload(photos)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post('/api/ai/v1/video-jobs', headers=headers(), json=body), range(4)))
    assert [r.status_code for r in responses] == [202]*4
    with jobs.connection() as conn:
        assert conn.execute('SELECT count(*) AS n FROM video_jobs').fetchone()['n'] == 1
    body['memory_context'] = 'changed'
    assert client.post('/api/ai/v1/video-jobs', headers=headers(), json=body).status_code == 409


def test_ownership_and_completion(api):
    client, photos = api
    body = payload(photos)
    assert client.post('/api/ai/v1/video-jobs', headers=headers(2), json=body).status_code == 404
    assert client.post('/api/ai/v1/video-jobs', headers=headers(), json=body).status_code == 202
    url = '/api/ai/v1/video-jobs/' + body['request_id']
    assert client.get(url, headers=headers(2)).status_code == 404
    assert client.get(url+'/video', headers=headers()).status_code == 409
    jobs.update(body['request_id'], 'GENERATING_VIDEO')
    assert client.get(url, headers=headers()).json()['status'] == 'RUNNING'
    jobs.update(body['request_id'], 'COMPLETED', status='COMPLETED', publication={'assets': {'video': {'key': 'test/final.mp4'}}})
    assert client.get(url, headers=headers()).json()['video_url'] == url+'/video'


def test_pending_quota(api):
    client, photos = api
    for _ in range(3):
        assert client.post('/api/ai/v1/video-jobs', headers=headers(), json=payload(photos)).status_code == 202
    assert client.post('/api/ai/v1/video-jobs', headers=headers(), json=payload(photos)).status_code == 429


def test_single_worker_lock(api):
    with jobs.connection() as first, jobs.connection() as second:
        assert first.execute('SELECT pg_try_advisory_lock(41420261104) AS locked').fetchone()['locked']
        assert not second.execute('SELECT pg_try_advisory_lock(41420261104) AS locked').fetchone()['locked']
    with jobs.connection() as third:
        assert third.execute('SELECT pg_try_advisory_lock(41420261104) AS locked').fetchone()['locked']


def test_job_records_travel_photos_and_completion(api):
    client, photos = api
    body = payload(photos)
    assert client.post('/api/ai/v1/video-jobs', headers=headers(), json=body).status_code == 202
    with jobs.connection() as conn:
        row = conn.execute('SELECT * FROM video_jobs WHERE id=%s', (body['request_id'],)).fetchone()
    assert row['travel_plan_id'] == 101
    assert row['photo_keys'] == [f'test/{p}' for p in photos]
    assert row['completed_at'] is None
    jobs.update(body['request_id'], 'COMPLETED', status='COMPLETED',
                publication={'assets': {'video': {'key': 'jobs/1/final.mp4'}}})
    data = client.get('/api/ai/v1/video-jobs/' + body['request_id'], headers=headers()).json()
    assert data['stage_label'] == '완료' and data['completed_at'] is not None
    with jobs.connection() as conn:
        assert conn.execute('SELECT video_key FROM video_jobs WHERE id=%s', (body['request_id'],)).fetchone()['video_key'] == 'jobs/1/final.mp4'


def test_failure_keeps_reason(api):
    client, photos = api
    body = payload(photos)
    client.post('/api/ai/v1/video-jobs', headers=headers(), json=body)
    jobs.update(body['request_id'], 'FAILED', status='FAILED', error='VIDEO_GENERATION_FAILED', message='comfyui_not_ready')
    data = client.get('/api/ai/v1/video-jobs/' + body['request_id'], headers=headers()).json()
    assert (data['status'], data['stage_label'], data['error_message']) == ('FAILED', '실패', 'comfyui_not_ready')


def test_interrupted_job_is_requeued_until_attempt_limit(api):
    client, photos = api
    body = payload(photos)
    client.post('/api/ai/v1/video-jobs', headers=headers(), json=body)
    for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
        with jobs.connection() as conn:
            claimed = jobs.claim(conn)
            assert claimed['attempts'] == attempt and claimed['stage'] == 'ANALYZING_PHOTOS'
        with jobs.connection() as conn:
            jobs.recover_interrupted(conn)
            status = conn.execute('SELECT status FROM video_jobs WHERE id=%s', (body['request_id'],)).fetchone()['status']
        assert status == ('QUEUED' if attempt < jobs.MAX_ATTEMPTS else 'FAILED')


def test_restart_keeps_jobs(api):
    client, photos = api
    body = payload(photos)
    client.post('/api/ai/v1/video-jobs', headers=headers(), json=body)
    jobs.initialize()  # A server restart re-runs schema setup; it must not drop rows.
    assert client.get('/api/ai/v1/video-jobs/' + body['request_id'], headers=headers()).json()['status'] == 'QUEUED'


def test_queue_position_counts_earlier_active_jobs(api):
    client, photos = api
    first, second = payload(photos), payload(photos)
    client.post('/api/ai/v1/video-jobs', headers=headers(), json=first)
    client.post('/api/ai/v1/video-jobs', headers=headers(), json=second)
    assert client.get('/api/ai/v1/video-jobs/' + first['request_id'], headers=headers()).json()['queue_position'] == 1
    assert client.get('/api/ai/v1/video-jobs/' + second['request_id'], headers=headers()).json()['queue_position'] == 2
