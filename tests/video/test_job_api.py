import io
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
    assert f'/api/ai/v1/playback/{job_id}?sig=' in response.headers['location']


def test_video_url_can_be_returned_as_json(monkeypatch):
    monkeypatch.setenv('AUDIGO_API_TOKEN', 'test-secret')
    monkeypatch.setenv('VIDEO_PUBLIC_BASE_URL', 'https://video.example.test/')
    monkeypatch.setattr(video_jobs, 'find_job', lambda *a: {'status': 'COMPLETED', 'video_key': 'jobs/private/final.mp4'})
    job_id = uuid4()
    response = client().get(f'/api/ai/v1/video-jobs/{job_id}/video?response_format=json',
                           headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    data = response.json()
    assert response.status_code == 200 and data['expires_in'] is None
    assert data['url'].startswith(f'https://video.example.test/api/ai/v1/playback/{job_id}?sig=')
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


def signed(job_id):
    return f'/api/ai/v1/playback/{job_id}?sig={video_jobs.playback_signature(job_id)}'


def test_signed_playback_streams_whole_file_and_ranges(monkeypatch):
    http = playback_client(monkeypatch)
    url = signed(uuid4())
    whole = http.get(url)
    assert whole.status_code == 200 and whole.content == VIDEO
    assert whole.headers['accept-ranges'] == 'bytes' and whole.headers['content-type'] == 'video/mp4'
    part = http.get(url, headers={'Range': 'bytes=100-199'})
    assert part.status_code == 206 and part.content == VIDEO[100:200]
    assert part.headers['content-range'] == f'bytes 100-199/{len(VIDEO)}'
    tail = http.get(url, headers={'Range': 'bytes=-10'})
    assert tail.status_code == 206 and tail.content == VIDEO[-10:]
    assert http.get(url, headers={'Range': f'bytes={len(VIDEO)}-'}).status_code == 416


def test_playback_link_is_permanent_but_rejects_tampering(monkeypatch):
    http = playback_client(monkeypatch)
    job_id = uuid4()
    good = signed(job_id)
    assert http.get(good).status_code == 200
    assert http.get(good.replace(str(job_id), str(uuid4()))).status_code == 403
    assert http.get(good[:-4] + '0000').status_code == 403
    assert http.get(f'/api/ai/v1/playback/{job_id}').status_code == 422



def png_bytes():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (10, 20, 30)).save(buffer, 'PNG')
    return buffer.getvalue()


def test_batch_upload_stores_each_photo_and_reports_failures(monkeypatch):
    saved = []
    def fake_save(data, owner):
        if not data.startswith(b'\x89PNG'):
            raise video_jobs.HTTPException(400, 'Invalid image')
        saved.append((len(data), owner))
        return {'upload_id': f'id-{len(saved)}'}
    monkeypatch.setattr(video_jobs, 'save_upload', fake_save)
    headers = {'Authorization': 'Bearer test-secret', 'X-Owner-ID': '7'}
    photo = png_bytes()
    ok = client().post('/api/ai/v1/uploads/batch', headers=headers,
                       files=[('files', ('a.png', photo, 'image/png')), ('files', ('b.png', photo, 'image/png'))])
    assert ok.status_code == 201
    assert ok.json() == {'upload_ids': ['id-1', 'id-2'], 'items': [
        {'filename': 'a.png', 'upload_id': 'id-1'}, {'filename': 'b.png', 'upload_id': 'id-2'}]}
    assert saved == [(len(photo), 7), (len(photo), 7)]
    mixed = client().post('/api/ai/v1/uploads/batch', headers=headers,
                          files=[('files', ('c.png', photo, 'image/png')), ('files', ('notes.txt', b'hello', 'text/plain'))])
    assert mixed.status_code == 207
    assert mixed.json()['upload_ids'] == ['id-3']
    assert mixed.json()['items'][1] == {'filename': 'notes.txt', 'error': 'Invalid image', 'status_code': 400}


def test_batch_upload_limits_file_count(monkeypatch):
    monkeypatch.setattr(video_jobs, 'save_upload', lambda data, owner: {'upload_id': 'x'})
    files = [('files', (f'{i}.png', b'x', 'image/png')) for i in range(video_jobs.MAX_BATCH_FILES + 1)]
    response = client().post('/api/ai/v1/uploads/batch', files=files,
                             headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    assert response.status_code == 400


def test_phone_mpo_photo_is_accepted(monkeypatch):
    from PIL import Image
    buffer = io.BytesIO()
    frames = [Image.new('RGB', (16, 16), (200, 100, 50)), Image.new('RGB', (8, 8))]
    frames[0].save(buffer, 'MPO', save_all=True, append_images=frames[1:])
    assert Image.open(io.BytesIO(buffer.getvalue())).format == 'MPO'
    stored = []
    s3 = SimpleNamespace(put_object=lambda **kw: stored.append(kw['Key']), delete_object=lambda **kw: None)
    monkeypatch.setattr(video_jobs, 'store', lambda: SimpleNamespace(settings=SimpleNamespace(volume_id='v'), client=s3))
    @contextmanager
    def conn():
        yield SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: {'n': 0}))
    monkeypatch.setattr(video_jobs, 'connection', conn)
    assert 'upload_id' in video_jobs.save_upload(buffer.getvalue(), 1)
    assert stored and stored[0].startswith('uploads/1/')
