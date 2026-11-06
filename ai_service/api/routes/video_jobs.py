"""백엔드의 여행 사진 업로드, 영상 작업, GPU 선택, 결과 재생 API를 제공한다."""
import asyncio
import hashlib
import hmac
import io
import os
from pathlib import Path
import re
from uuid import UUID, uuid4
from typing import Annotated, Literal

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.gpu_power import AUTO_GPU, GPU_LABELS, default_gpu_type, gpu_selection_allowed, selectable_gpu_types
from ai_service.services.video.jobs import STAGES, cancel, connection, failure_message
from ai_service.services.video.storage import VolumeResults, VolumeSettings

Owner = Annotated[int, Header(alias='X-Owner-ID', gt=0, le=9223372036854775807)]
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_BATCH_FILES = 30
TEST_PAGE = Path(__file__).resolve().parents[1] / 'static' / 'video-test.html'

class JobInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    travel_plan_id: int = Field(gt=0, le=9223372036854775807, strict=True)
    upload_ids: list[UUID] = Field(min_length=2, max_length=120)
    duration_seconds: int = Field(default=30, ge=5, le=30)
    memory_context: str = Field(default='', max_length=500)
    memory_style: Literal['WARM', 'LIVELY', 'CINEMATIC'] = 'WARM'
    music: Literal['AUTO', 'WARM', 'BRIGHT', 'CALM'] = 'AUTO'
    gpu_type: str | None = Field(default=None, min_length=1, max_length=120)


SERVERLESS_GPU_LABEL = os.environ.get('VIDEO_SERVERLESS_GPU_LABEL', 'A100 80GB · RTX PRO 6000 96GB · H100 80GB 중 빈 GPU 1대 (서버리스 자동)')


def store():
    return VolumeResults(VolumeSettings.from_env())


def find_job(job_id, owner):
    with connection() as conn:
        row = conn.execute('SELECT * FROM video_jobs WHERE id=%s AND owner_id=%s', (job_id, owner)).fetchone()
    if not row:
        raise HTTPException(404, 'Job not found')
    return row


WORKER_LABELS = {
    'IDLE': '작업 실행기 대기 중',
    'CHECKING_GPU': 'GPU 연결 확인 중',
    'WAITING_FOR_GPU': 'GPU가 꺼져 있어 기다리는 중',
    'STARTING_GPU': 'GPU를 켜는 중 (모델 내려받기 포함 약 4~5분)',
    'GPU_SOLD_OUT': 'GPU 품절: RunPod에 빈 GPU가 없어 자리가 날 때까지 기다리는 중',
    'WAITING_FOR_GPU_CAPACITY': '선택한 GPU의 사용 가능한 자리를 기다리는 중',
    'GPU_MODELS_MISSING': 'GPU는 켜졌지만 영상 모델이 없어 기다리는 중',
    'GPU_BUSY': 'GPU가 다른 작업을 처리하는 중',
    'BUSY': '영상 생성 중',
    'OFFLINE': '작업 실행기가 응답하지 않음',
}
# One measured end-to-end run on an A100: 110 photos, 30-second film, 1,679 s. Not a guarantee.
JOB_MINUTES = 30


def estimate_note(status, position, worker):
    """Plain-language wait and run estimate; jobs run one at a time on one GPU."""
    if status == 'RUNNING':
        return f'만들기 시작한 뒤 약 {JOB_MINUTES}분 걸립니다 (실측 1회: 사진 110장·30초 영상 28분).'
    ahead = (position or 1) - 1
    wait = f'앞에 {ahead}개 작업이 있어 시작까지 약 {ahead * JOB_MINUTES}분, ' if ahead else ''
    boot = ' GPU가 꺼져 있으면 켜는 데 몇 분이 더 걸립니다.' if worker in ('WAITING_FOR_GPU', 'STARTING_GPU') else ''
    if worker == 'GPU_SOLD_OUT':
        boot = ' 지금은 허용한 GPU가 모두 품절이라, 빈 GPU가 생기면 바로 시작합니다.'
    if worker == 'WAITING_FOR_GPU_CAPACITY':
        boot = ' GPU를 할당할 수 있을 때 시작합니다.'
    return f'{wait}시작 후 약 {JOB_MINUTES}분 걸립니다.{boot}'


def queue_position(job_id):
    """1 means next in line; counts queued and running jobs submitted earlier by anyone."""
    with connection() as conn:
        row = conn.execute("""SELECT count(*) AS ahead FROM video_jobs
            WHERE status IN ('QUEUED','RUNNING') AND created_at < (SELECT created_at FROM video_jobs WHERE id=%s)""",
                           (job_id,)).fetchone()
    return row['ahead'] + 1


def korean_failure(row):
    """Jobs failed before Korean messages stored the raw code (e.g. 'score_quality_failed') as the message."""
    message = row.get('error_message')
    if message and not message.isascii():
        return message
    return failure_message(message if message and re.fullmatch(r'[A-Za-z0-9_]+', message) else row.get('error_code'))


def worker_state():
    with connection() as conn:
        worker = conn.execute("SELECT state, heartbeat_at > now() - interval '30 seconds' AS alive FROM video_worker WHERE id=1").fetchone()
    return worker['state'] if worker and worker['alive'] else 'OFFLINE'


def save_upload(data, owner):
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as photo:
            # MPO is the JPEG variant phones save with depth or preview frames; its first frame is a normal JPEG.
            if photo.format not in {'JPEG', 'MPO', 'PNG', 'WEBP'} or photo.width * photo.height > 40_000_000:
                raise HTTPException(400, 'Unsupported image or image too large')
            photo.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, 'Invalid image') from exc
    upload_id = uuid4()
    key = f'uploads/{owner}/{upload_id}.image'
    volume = store()
    uploaded = False
    try:
        with connection() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (owner,))
            count = conn.execute('SELECT count(*) AS n FROM video_uploads WHERE owner_id=%s', (owner,)).fetchone()['n']
            if count >= 1000:
                raise HTTPException(429, 'Upload quota reached; contact administrator')
            volume.client.put_object(Bucket=volume.settings.volume_id, Key=key, Body=data,
                                     ContentType='application/octet-stream')
            uploaded = True
            conn.execute('INSERT INTO video_uploads(id,owner_id,storage_key) VALUES (%s,%s,%s)',
                         (upload_id, owner, key))
    except Exception:
        if uploaded:
            volume.client.delete_object(Bucket=volume.settings.volume_id, Key=key)
        raise
    return {'upload_id': str(upload_id)}


def router(settings):
    routes = APIRouter(tags=['V3 Video'])

    @routes.get('/api/ai/v1/video-gpu-options', summary='영상 생성 GPU 선택지')
    def gpu_options():
        settings = VideoSettings.from_env()
        if settings.endpoint_mode == 'serverless':
            # One fixed GPU pool on the RunPod serverless endpoint: nothing to choose.
            return {'default_gpu_type': None, 'items': [], 'serverless': {
                'label': SERVERLESS_GPU_LABEL,
                'note': '서버리스 GPU를 씁니다. 요청이 들어오면 RunPod가 위 순서대로 비어 있는 GPU를 자동으로 켜고, 10분 동안 일이 없으면 끕니다. '
                        '꺼져 있던 GPU가 켜지는 데 몇 분 걸리고, GPU가 일한 시간만큼만 요금이 나갑니다.'}}
        models = selectable_gpu_types(settings)
        choices = (*models, AUTO_GPU) if len(models) > 1 else models
        return {'default_gpu_type': default_gpu_type(models),
                'items': [{'id': model, 'label': GPU_LABELS.get(model, f'{model} ×1')} for model in choices]}

    # Declared so the /docs page offers a file picker for the raw image body.
    @routes.post('/api/ai/v1/uploads', status_code=201, summary='사진 1장 업로드', description='한 번에 1장만 올립니다. 여러 장은 `사진 여러 장 업로드`나 [테스트 페이지](/video-test)를 쓰세요.', openapi_extra={'requestBody': {'required': True, 'content': {
        kind: {'schema': {'type': 'string', 'format': 'binary'}} for kind in ('image/jpeg', 'image/png', 'image/webp')}}})
    async def upload(request: Request, x_owner_id: Owner):
        # Raw image body avoids multipart buffering and requires no multipart dependency.
        data = bytearray()
        try:
            async with asyncio.timeout(120):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, 'Maximum image size is 20 MiB')
                    data.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, 'Upload timed out') from None
        return await asyncio.to_thread(save_upload, bytes(data), x_owner_id)

    @routes.post('/api/ai/v1/uploads/batch', status_code=201, summary='사진 여러 장 업로드 (최대 30장)')
    async def upload_batch(x_owner_id: Owner, response: Response,
                           files: Annotated[list[UploadFile], File(description='사진 최대 30장 (JPEG·PNG·WebP·아이폰 MPO). Swagger에서는 칸 하나에 파일 1개만 고를 수 있으니 "Add string item"으로 칸을 늘려 사진마다 하나씩 선택하세요.')]):
        """Several photos in one multipart request; each is stored independently so failed ones can be retried."""
        if not 1 <= len(files) <= MAX_BATCH_FILES:
            raise HTTPException(400, f'Send between 1 and {MAX_BATCH_FILES} photos per request')
        items = []
        for photo in files:
            data = await photo.read(MAX_UPLOAD_BYTES + 1)
            try:
                if len(data) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, 'Maximum image size is 20 MiB')
                saved = await asyncio.to_thread(save_upload, data, x_owner_id)
                items.append({'filename': photo.filename, 'upload_id': saved['upload_id']})
            except HTTPException as exc:
                items.append({'filename': photo.filename, 'error': exc.detail, 'status_code': exc.status_code})
        upload_ids = [item['upload_id'] for item in items if 'upload_id' in item]
        if len(upload_ids) != len(items):
            response.status_code = 207  # Multi-Status: some photos failed; retry only those.
        return {'upload_ids': upload_ids, 'items': items}

    @routes.post('/api/ai/v1/video-jobs', status_code=202, summary='영상 생성 요청')
    def create(body: JobInput, x_owner_id: Owner):
        from psycopg.types.json import Jsonb
        if len(set(body.upload_ids)) != len(body.upload_ids):
            raise HTTPException(400, 'Duplicate upload IDs')
        # GPU를 생략한 기존 요청과 같은 JSON을 유지해 멱등성 검사가 계속 동작하게 한다.
        payload = body.model_dump(mode='json', exclude_none=True)
        with connection() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (x_owner_id,))
            existing = conn.execute('SELECT * FROM video_jobs WHERE id=%s', (body.request_id,)).fetchone()
            if existing is None:
                settings = VideoSettings.from_env()
                # A serverless endpoint has one GPU pool; the worker ignores a picked GPU there.
                if (body.gpu_type is not None and settings.endpoint_mode != 'serverless'
                        and not gpu_selection_allowed(body.gpu_type, selectable_gpu_types(settings))):
                    raise HTTPException(422, '이 서버에서 선택할 수 없는 GPU 모델입니다.')
                pending = conn.execute("SELECT count(*) AS n FROM video_jobs WHERE owner_id=%s AND status IN ('QUEUED','RUNNING')", (x_owner_id,)).fetchone()['n']
                if pending >= 3:
                    raise HTTPException(429, 'At most three pending jobs per user')
            found = conn.execute('SELECT id,storage_key FROM video_uploads WHERE owner_id=%s AND id=ANY(%s)',
                                 (x_owner_id, body.upload_ids)).fetchall()
            if len(found) != len(body.upload_ids):
                raise HTTPException(404, 'Upload not found')
            keys = {row['id']: row['storage_key'] for row in found}
            conn.execute('''INSERT INTO video_jobs(id,owner_id,request,travel_plan_id,photo_keys) VALUES(%s,%s,%s,%s,%s)
                            ON CONFLICT(id) DO NOTHING''',
                         (body.request_id, x_owner_id, Jsonb(payload), body.travel_plan_id,
                          [keys[upload_id] for upload_id in body.upload_ids]))
            row = conn.execute('SELECT * FROM video_jobs WHERE id=%s', (body.request_id,)).fetchone()
            if row['owner_id'] != x_owner_id or row['request'] != payload:
                raise HTTPException(409, 'Request ID already used')
        return {'job_id': str(body.request_id), 'status': row['status'],
                'status_url': f'/api/ai/v1/video-jobs/{body.request_id}'}

    @routes.post('/api/ai/v1/video-jobs/{job_id}/cancel', summary='영상 생성 취소',
                 description='대기 중이면 바로 취소하고, 만드는 중이면 작업 실행기가 GPU 작업까지 멈춘 뒤 취소합니다.')
    def cancel_job(job_id: UUID, x_owner_id: Owner):
        result = cancel(job_id, x_owner_id)
        if result is None:
            raise HTTPException(404, 'Job not found')
        if result == 'FINISHED':
            raise HTTPException(409, 'Job already finished')
        message = '취소했습니다.' if result == 'CANCELLED' else '취소를 요청했습니다. 만들던 작업을 멈추는 중입니다 (보통 수십 초).'
        return JSONResponse({'job_id': str(job_id), 'status': result, 'message': message},
                            status_code=200 if result == 'CANCELLED' else 202)

    @routes.get('/api/ai/v1/video-jobs', summary='내 영상 작업 목록')
    def list_jobs(x_owner_id: Owner, offset: int = 0,
                  travel_plan_id: Annotated[int | None, Query(gt=0, description='이 여행의 작업만 (백엔드가 작업번호를 따로 저장하지 않아도 됨)')] = None):
        if offset < 0:
            raise HTTPException(400, 'Invalid offset')
        with connection() as conn:
            rows = conn.execute('''SELECT id,travel_plan_id,status,stage,error_code,error_message,notice,created_at,completed_at,
                                          updated_at,coalesce(cardinality(photo_keys), 0) AS photo_count
                                   FROM video_jobs WHERE owner_id=%s AND (%s::bigint IS NULL OR travel_plan_id=%s)
                                   ORDER BY created_at DESC LIMIT 50 OFFSET %s''',
                                (x_owner_id, travel_plan_id, travel_plan_id, offset)).fetchall()
        items = [{**row, 'stage_label': STAGES.get(row['stage'], row['stage']), 'error_message': korean_failure(row),
                  'video_url': f"/api/ai/v1/video-jobs/{row['id']}/video" if row['status'] == 'COMPLETED' else None}
                 for row in rows]
        return {'items': items, 'offset': offset, 'limit': 50}

    @routes.get('/api/ai/v1/video-jobs/{job_id}', summary='영상 진행 상태 조회')
    def status(job_id: UUID, x_owner_id: Owner):
        row = find_job(job_id, x_owner_id)
        worker = worker_state()
        active = row['status'] in ('QUEUED', 'RUNNING')
        request_body = row.get('request') or {}
        position = queue_position(job_id) if row['status'] == 'QUEUED' else None
        worker_label = WORKER_LABELS.get(worker, worker)
        if position and position > 1 and worker == 'BUSY':
            worker_label = '앞 순서의 다른 영상을 만드는 중 (이 작업은 그다음 차례)'
        cancelling = row['status'] == 'RUNNING' and row.get('cancel_requested')
        return {'job_id': str(job_id), 'status': row['status'], 'stage': row['stage'],
                'stage_label': '취소하는 중' if cancelling else STAGES.get(row['stage'], row['stage']),
                'cancel_requested': bool(row.get('cancel_requested')),
                'stage_times': row.get('stage_times') or {},
                'travel_plan_id': row.get('travel_plan_id'),
                'photo_count': len(row.get('photo_keys') or request_body.get('upload_ids') or []),
                'duration_seconds': request_body.get('duration_seconds'),
                'gpu_type': request_body.get('gpu_type'),
                'assigned_gpu_type': row.get('assigned_gpu_type'),
                'queue_position': position,
                'worker_state': worker, 'worker_state_label': worker_label,
                'estimate_note': estimate_note(row['status'], position, worker) if active else None,
                'error_code': row['error_code'], 'error_message': korean_failure(row),
                'notice': row.get('notice'),
                'progress_detail': row.get('progress_detail') if row['status'] == 'RUNNING' else None,
                'created_at': row.get('created_at'), 'completed_at': row.get('completed_at'),
                'updated_at': row['updated_at'],
                'video_url': f'/api/ai/v1/video-jobs/{job_id}/video' if row['status'] == 'COMPLETED' else None}

    @routes.get('/api/ai/v1/video-jobs/{job_id}/video', summary='영상 재생 주소 발급 (만료 없음)')
    def video(job_id: UUID, x_owner_id: Owner, request: Request,
              response_format: Literal['redirect', 'json'] = 'redirect'):
        row = find_job(job_id, x_owner_id)
        if row['status'] != 'COMPLETED':
            raise HTTPException(409, 'Video is not completed')
        # RunPod's S3 gateway rejects presigned URLs, so the service signs its own permanent link.
        # Changing VIDEO_PLAYBACK_SECRET invalidates every link issued so far.
        base = os.environ.get('VIDEO_PUBLIC_BASE_URL') or str(request.base_url)
        url = f"{base.rstrip('/')}/api/ai/v1/playback/{job_id}?sig={playback_signature(job_id)}"
        if response_format == 'json':
            return JSONResponse({'url': url, 'expires_in': None}, headers={'Cache-Control': 'no-store'})
        return RedirectResponse(url, status_code=307, headers={'Cache-Control': 'no-store'})

    return routes


def playback_signature(job_id):
    secret = os.environ.get('VIDEO_PLAYBACK_SECRET') or os.environ['AUDIGO_API_TOKEN']
    key = hashlib.sha256(b'audigo-playback:' + secret.encode()).digest()
    return hmac.new(key, f'permanent:{job_id}'.encode(), hashlib.sha256).hexdigest()


def parse_range(header, size):
    """Single `bytes=start-end` range as browsers send for seeking; None means the whole file."""
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header or '')
    if not match or match.groups() == ('', ''):
        return None
    start, end = match.groups()
    if start == '':
        start, end = max(size - int(end), 0), size - 1
    else:
        start, end = int(start), min(int(end), size - 1) if end else size - 1
    if start > end or start >= size:
        raise HTTPException(416, 'Range not satisfiable', headers={'Content-Range': f'bytes */{size}'})
    return start, end


def playback_router():
    """Browser-facing stream; the signed URL is the authorization, so no service token is needed."""
    routes = APIRouter(tags=['V3 Video'])

    @routes.get('/video-test', include_in_schema=False)
    def test_page():
        # Swagger can attach only one file per row; this page uses a real multi-select file picker.
        return HTMLResponse(TEST_PAGE.read_text(encoding='utf-8'), headers={'Cache-Control': 'no-store'})

    @routes.get('/api/ai/v1/playback/{job_id}', include_in_schema=False)
    def playback(job_id: UUID, sig: str, range: Annotated[str | None, Header()] = None):
        if not hmac.compare_digest(sig, playback_signature(job_id)):
            raise HTTPException(403, 'Playback link invalid')
        with connection() as conn:
            row = conn.execute("SELECT video_key FROM video_jobs WHERE id=%s AND status='COMPLETED'", (job_id,)).fetchone()
        if not row or not row['video_key']:
            raise HTTPException(404, 'Video not found')
        volume = store()
        size = volume.client.head_object(Bucket=volume.settings.volume_id, Key=row['video_key'])['ContentLength']
        window = parse_range(range, size)
        params = {'Bucket': volume.settings.volume_id, 'Key': row['video_key']}
        headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'private, no-store'}
        if window:
            params['Range'] = f'bytes={window[0]}-{window[1]}'
            headers['Content-Range'] = f'bytes {window[0]}-{window[1]}/{size}'
        body = volume.client.get_object(**params)['Body']
        headers['Content-Length'] = str(window[1] - window[0] + 1 if window else size)
        return StreamingResponse(body.iter_chunks(1024 * 1024), status_code=206 if window else 200,
                                 media_type='video/mp4', headers=headers)

    return routes
