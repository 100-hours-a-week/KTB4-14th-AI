import pytest
import io
from contextlib import contextmanager
import time
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
from ai_service.api.routes import video_jobs
from ai_service.core.auth import require_api_token
from ai_service.services.video.gpu_power import A100_PCIE, AUTO_GPU, RTX_PRO_6000
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


def test_new_video_job_rejects_silent_music_choice():
    response = client().post('/api/ai/v1/video-jobs',
                             headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'},
                             json={'request_id': str(uuid4()), 'travel_plan_id': 1,
                                   'upload_ids': [str(uuid4()), str(uuid4())], 'music': 'NONE'})
    assert response.status_code == 422
    assert response.json()['detail'][0]['loc'] == ['body', 'music']


def test_gpu_choices_and_default_are_controlled_by_the_worker_configuration(monkeypatch):
    monkeypatch.setenv('VIDEO_GPU_AUTOSTART', '1')
    monkeypatch.setenv('VIDEO_ENDPOINT_MODE', 'runpod')
    monkeypatch.setenv('VIDEO_GPU_POD_ID', '')
    monkeypatch.setenv('VIDEO_GPU_TYPES', f'{A100_PCIE},{RTX_PRO_6000}')
    response = client().get('/api/ai/v1/video-gpu-options', headers={'Authorization': 'Bearer test-secret'})
    assert response.status_code == 200
    assert response.json()['default_gpu_type'] == RTX_PRO_6000
    assert {item['id'] for item in response.json()['items']} == {RTX_PRO_6000, A100_PCIE, AUTO_GPU}

    monkeypatch.setenv('VIDEO_GPU_POD_ID', 'fixed')
    assert client().get('/api/ai/v1/video-gpu-options', headers={'Authorization': 'Bearer test-secret'}).json()['items'] == []


def test_unapproved_gpu_is_rejected_before_upload_lookup(monkeypatch):
    @contextmanager
    def no_existing_job():
        def execute(sql, params):
            assert 'pg_advisory_xact_lock' in sql or 'SELECT * FROM video_jobs' in sql
            return SimpleNamespace(fetchone=lambda: None)
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(video_jobs, 'connection', no_existing_job)
    monkeypatch.setenv('VIDEO_GPU_AUTOSTART', '1')
    monkeypatch.setenv('VIDEO_ENDPOINT_MODE', 'runpod')
    monkeypatch.setenv('VIDEO_GPU_TYPES', RTX_PRO_6000)
    response = client().post('/api/ai/v1/video-jobs',
                             headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'},
                             json={'request_id': str(uuid4()), 'travel_plan_id': 1,
                                   'upload_ids': [str(uuid4()), str(uuid4())], 'gpu_type': A100_PCIE})
    assert response.status_code == 422


def test_invalid_image_is_rejected_before_storage():
    response = client().post('/api/ai/v1/uploads', content=b'not a photograph',
                            headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    assert response.status_code == 400


def test_completion_url_is_only_shown_when_completed(monkeypatch):
    monkeypatch.setattr(video_jobs, 'worker_state', lambda: 'BUSY')
    monkeypatch.setattr(video_jobs, 'queue_position', lambda job: 2)
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
            raise video_jobs.VideoError(400, 'invalid_image', 'Invalid image')
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
    assert mixed.json()['items'][1] == {'filename': 'notes.txt', 'status_code': 400, 'code': 'invalid_image', 'error': 'Invalid image'}


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


def test_status_explains_queue_and_estimate(monkeypatch):
    monkeypatch.setattr(video_jobs, 'worker_state', lambda: 'WAITING_FOR_GPU')
    monkeypatch.setattr(video_jobs, 'queue_position', lambda job: 2)
    row = {'status': 'QUEUED', 'stage': 'QUEUED', 'error_code': None, 'updated_at': None,
           'photo_keys': ['a', 'b', 'c'], 'request': {'duration_seconds': 30}}
    monkeypatch.setattr(video_jobs, 'find_job', lambda job, owner: row)
    data = client().get(f'/api/ai/v1/video-jobs/{uuid4()}', headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'}).json()
    assert (data['queue_position'], data['photo_count'], data['duration_seconds']) == (2, 3, 30)
    assert data['worker_state_label'] == 'GPU가 꺼져 있어 기다리는 중'
    assert '앞에 1개 작업' in data['estimate_note'] and '켜는 데' in data['estimate_note']
    row.update(status='COMPLETED', stage='COMPLETED')
    done = client().get(f'/api/ai/v1/video-jobs/{uuid4()}', headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'}).json()
    assert done['queue_position'] is None and done['estimate_note'] is None


def test_failure_reason_is_shown_in_korean(monkeypatch):
    from ai_service.services.video.jobs import UNKNOWN_FAILURE, failure_message
    assert '배경음악' in failure_message('score_quality_failed')
    assert '사진' in failure_message('photo_decode_failed')
    assert failure_message('something_new') == UNKNOWN_FAILURE and failure_message(None) is None
    monkeypatch.setattr(video_jobs, 'worker_state', lambda: 'IDLE')
    monkeypatch.setattr(video_jobs, 'queue_position', lambda job: None)
    headers = {'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'}
    # Jobs failed before this change stored the raw code as the message.
    old = {'status': 'FAILED', 'stage': 'FAILED', 'error_code': 'VIDEO_GENERATION_FAILED', 'error_message': 'score_quality_failed', 'updated_at': None}
    monkeypatch.setattr(video_jobs, 'find_job', lambda job, owner: old)
    assert '배경음악' in client().get(f'/api/ai/v1/video-jobs/{uuid4()}', headers=headers).json()['error_message']
    korean = {**old, 'error_code': 'WORKER_INTERRUPTED', 'error_message': '작업 실행기가 반복해서 중단되어 생성을 포기했습니다.'}
    monkeypatch.setattr(video_jobs, 'find_job', lambda job, owner: korean)
    assert client().get(f'/api/ai/v1/video-jobs/{uuid4()}', headers=headers).json()['error_message'] == korean['error_message']


def test_waiting_behind_another_job_is_explained(monkeypatch):
    monkeypatch.setattr(video_jobs, 'worker_state', lambda: 'BUSY')
    monkeypatch.setattr(video_jobs, 'queue_position', lambda job: 2)
    row = {'status': 'QUEUED', 'stage': 'QUEUED', 'error_code': None, 'updated_at': None, 'photo_keys': ['a'], 'request': {}}
    monkeypatch.setattr(video_jobs, 'find_job', lambda job, owner: row)
    data = client().get(f'/api/ai/v1/video-jobs/{uuid4()}', headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'}).json()
    assert '앞 순서의 다른 영상' in data['worker_state_label']
    assert data['estimate_note'].startswith('앞에 1개 작업이 있어 시작까지 약 10분')


@pytest.mark.parametrize('result, status_code, status', [('CANCELLED', 200, 'CANCELLED'), ('CANCELLING', 202, 'CANCELLING')])
def test_cancel_endpoint(monkeypatch, result, status_code, status):
    calls = []
    monkeypatch.setattr(video_jobs, 'cancel', lambda job, owner: calls.append((str(job), owner)) or result)
    job_id = uuid4()
    response = client().post(f'/api/ai/v1/video-jobs/{job_id}/cancel', headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '3'})
    assert (response.status_code, response.json()['status']) == (status_code, status)
    assert calls == [(str(job_id), 3)]


@pytest.mark.parametrize('result, status_code', [(None, 404), ('FINISHED', 409)])
def test_cancel_unknown_or_finished_job(monkeypatch, result, status_code):
    monkeypatch.setattr(video_jobs, 'cancel', lambda job, owner: result)
    response = client().post(f'/api/ai/v1/video-jobs/{uuid4()}/cancel', headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'})
    assert response.status_code == status_code


def test_cancel_requires_service_token():
    assert client().post(f'/api/ai/v1/video-jobs/{uuid4()}/cancel', headers={'X-Owner-ID': '1'}).status_code == 401


def test_running_job_with_cancel_request_says_cancelling(monkeypatch):
    monkeypatch.setattr(video_jobs, 'worker_state', lambda: 'BUSY')
    row = {'status': 'RUNNING', 'stage': 'GENERATING_VIDEO', 'cancel_requested': True, 'error_code': None, 'updated_at': None}
    monkeypatch.setattr(video_jobs, 'find_job', lambda job, owner: row)
    data = client().get(f'/api/ai/v1/video-jobs/{uuid4()}', headers={'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'}).json()
    assert data['stage_label'] == '취소하는 중' and data['cancel_requested'] is True


def fake_storage(monkeypatch):
    stored = []
    s3 = SimpleNamespace(put_object=lambda **kw: stored.append(kw), delete_object=lambda **kw: None)
    monkeypatch.setattr(video_jobs, 'store', lambda: SimpleNamespace(settings=SimpleNamespace(volume_id='v'), client=s3))
    @contextmanager
    def conn():
        yield SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: {'n': 0}))
    monkeypatch.setattr(video_jobs, 'connection', conn)
    return stored


def test_iphone_heic_is_stored_as_upright_jpeg(monkeypatch):
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    photo = Image.new('RGB', (40, 20), (200, 100, 50))
    exif = photo.getexif()
    exif[0x0112] = 6  # iPhone portrait: rotate 90 degrees when shown
    buffer = io.BytesIO()
    photo.save(buffer, 'HEIF', exif=exif.tobytes())
    stored = fake_storage(monkeypatch)
    assert 'upload_id' in video_jobs.save_upload(buffer.getvalue(), 1)
    with Image.open(io.BytesIO(stored[0]['Body'])) as saved:
        assert saved.format == 'JPEG'
        assert saved.size == (20, 40)
        assert saved.getexif().get(0x0112) is None


def test_video_errors_have_distinct_codes(monkeypatch):
    from ai_service.core.handlers import register_exception_handlers
    fake_storage(monkeypatch)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(video_jobs.router(None), dependencies=[Depends(require_api_token('test-secret'))])
    http = TestClient(app)
    headers = {'Authorization': 'Bearer test-secret', 'X-Owner-ID': '1'}
    bad = http.post('/api/ai/v1/uploads', headers=headers, content=b'not an image')
    assert bad.status_code == 400
    assert bad.json()['message'] == 'invalid_image'
    assert bad.json()['data']['error_message']
    gif = io.BytesIO()
    from PIL import Image
    Image.new('RGB', (4, 4)).save(gif, 'GIF')
    unsupported = http.post('/api/ai/v1/uploads', headers=headers, content=gif.getvalue())
    assert unsupported.json()['message'] == 'unsupported_image_format'
