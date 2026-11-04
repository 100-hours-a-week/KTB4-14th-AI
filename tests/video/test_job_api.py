from contextlib import contextmanager
import time
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
from ai_service.api.routes import video_jobs
from ai_service.core.auth import require_api_token
from fastapi import Depends


def client():
    app = FastAPI()
    app.include_router(video_jobs.router(None), dependencies=[Depends(require_api_token('test-secret'))])
    return TestClient(app)


def test_private_api_rejects_missing_token():
    response = client().get(f'/api/ai/v1/video-jobs/{uuid4()}', headers={'X-Owner-ID': '1'})
    assert response.status_code == 401


def test_owner_header_required():
    response = client().get(f'/api/ai/v1/video-jobs/{uuid4()}', headers={'Authorization': 'Bearer test-secret'})
    assert response.status_code == 422


def test_invalid_image_is_rejected_before_storage():
    response = client().post('/api/ai/v1/uploads', content=b'not a photograph',
                            headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    assert response.status_code == 400


def test_completion_url_is_only_shown_when_completed(monkeypatch):
    monkeypatch.setattr(video_jobs, 'worker_state', lambda: 'BUSY')
    job_id = uuid4()
    row = {'status': 'RUNNING', 'stage': 'GENERATING_VIDEO', 'error_code': None, 'updated_at': None}
    monkeypatch.setattr(video_jobs, 'find_job', lambda job, owner: row)
    headers = {'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'}
    response = client().get(f'/api/ai/v1/video-jobs/{job_id}', headers=headers)
    assert response.json()['video_url'] is None
    row['status'] = 'COMPLETED'
    assert client().get(f'/api/ai/v1/video-jobs/{job_id}', headers=headers).json()['video_url'].endswith('/video')


def test_video_url_has_short_expiry_and_no_cache(monkeypatch):
    monkeypatch.setenv('AUDIGO_API_TOKEN', 'test-secret')
    monkeypatch.setattr(video_jobs, 'find_job', lambda *a: {'status': 'COMPLETED', 'video_key': 'jobs/private/final.mp4'})
    job_id = uuid4()
    response = client().get(f'/api/ai/v1/video-jobs/{job_id}/video', follow_redirects=False,
                           headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    assert response.status_code == 307
    assert response.headers['cache-control'] == 'no-store'
    assert f'/api/ai/v1/playback/{job_id}?exp=' in response.headers['location']


def test_video_url_can_be_returned_as_json(monkeypatch):
    monkeypatch.setenv('AUDIGO_API_TOKEN', 'test-secret')
    monkeypatch.setenv('VIDEO_PUBLIC_BASE_URL', 'https://video.example.test/')
    monkeypatch.setattr(video_jobs, 'find_job', lambda *a: {'status': 'COMPLETED', 'video_key': 'jobs/private/final.mp4'})
    job_id = uuid4()
    response = client().get(f'/api/ai/v1/video-jobs/{job_id}/video?response_format=json',
                           headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    data = response.json()
    assert response.status_code == 200 and data['expires_in'] == 300
    assert data['url'].startswith(f'https://video.example.test/api/ai/v1/playback/{job_id}?exp=')
    assert response.headers['cache-control'] == 'no-store'


VIDEO = bytes(range(256)) * 40  # 10,240 bytes


@contextmanager
def fake_connection():
    yield SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: {'video_key': 'jobs/x/final.mp4'}))


def playback_client(monkeypatch):
    monkeypatch.setenv('AUDIGO_API_TOKEN', 'test-secret')
    monkeypatch.setattr(video_jobs, 'connection', fake_connection)

    def get_object(Bucket, Key, Range=None):
        start, end = (int(x) for x in Range[6:].split('-')) if Range else (0, len(VIDEO) - 1)
        return {'Body': SimpleNamespace(iter_chunks=lambda size: iter([VIDEO[start:end + 1]]))}
    s3 = SimpleNamespace(head_object=lambda Bucket, Key: {'ContentLength': len(VIDEO)}, get_object=get_object)
    monkeypatch.setattr(video_jobs, 'store', lambda: SimpleNamespace(settings=SimpleNamespace(volume_id='volume'), client=s3))
    app = FastAPI()
    app.include_router(video_jobs.playback_router())
    return TestClient(app)


def signed(job_id, expires):
    return f'/api/ai/v1/playback/{job_id}?exp={expires}&sig={video_jobs.playback_signature(job_id, expires)}'


def test_signed_playback_streams_whole_file_and_ranges(monkeypatch):
    http = playback_client(monkeypatch)
    url = signed(uuid4(), int(time.time()) + 300)
    whole = http.get(url)
    assert whole.status_code == 200 and whole.content == VIDEO
    assert whole.headers['accept-ranges'] == 'bytes' and whole.headers['content-type'] == 'video/mp4'
    part = http.get(url, headers={'Range': 'bytes=100-199'})
    assert part.status_code == 206 and part.content == VIDEO[100:200]
    assert part.headers['content-range'] == f'bytes 100-199/{len(VIDEO)}'
    tail = http.get(url, headers={'Range': 'bytes=-10'})
    assert tail.status_code == 206 and tail.content == VIDEO[-10:]
    assert http.get(url, headers={'Range': f'bytes={len(VIDEO)}-'}).status_code == 416


def test_playback_rejects_expired_or_tampered_links(monkeypatch):
    http = playback_client(monkeypatch)
    job_id = uuid4()
    assert http.get(signed(job_id, int(time.time()) - 1)).status_code == 403
    good = signed(job_id, int(time.time()) + 300)
    assert http.get(good.replace(str(job_id), str(uuid4()))).status_code == 403
    assert http.get(good[:-4] + '0000').status_code == 403
