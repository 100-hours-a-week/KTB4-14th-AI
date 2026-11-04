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
    monkeypatch.setattr(video_jobs, 'find_job', lambda *a: {'status': 'COMPLETED', 'publication': {'assets': {'video': {'key': 'jobs/private/final.mp4'}}}})
    def sign(operation, *, Params, ExpiresIn):
        assert operation == 'get_object'
        assert Params['Key'] == 'jobs/private/final.mp4'
        assert ExpiresIn == 300
        return 'https://example.invalid/signed'
    monkeypatch.setattr(video_jobs, 'store', lambda: SimpleNamespace(settings=SimpleNamespace(volume_id='volume'), client=SimpleNamespace(generate_presigned_url=sign)))
    response = client().get(f'/api/ai/v1/video-jobs/{uuid4()}/video', follow_redirects=False,
                           headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    assert response.status_code == 307
    assert response.headers['cache-control'] == 'no-store'


def test_video_url_can_be_returned_as_json(monkeypatch):
    monkeypatch.setattr(video_jobs, 'find_job', lambda *a: {'status': 'COMPLETED', 'video_key': 'jobs/private/final.mp4'})
    monkeypatch.setattr(video_jobs, 'store', lambda: SimpleNamespace(settings=SimpleNamespace(volume_id='volume'), client=SimpleNamespace(
        generate_presigned_url=lambda op, *, Params, ExpiresIn: f"https://example.invalid/{Params['Key']}?exp={ExpiresIn}")))
    response = client().get(f'/api/ai/v1/video-jobs/{uuid4()}/video?response_format=json',
                           headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    assert response.status_code == 200
    assert response.json() == {'url': 'https://example.invalid/jobs/private/final.mp4?exp=300', 'expires_in': 300}
    assert response.headers['cache-control'] == 'no-store'
