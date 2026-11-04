"""Private backend-to-AI HTTP contract; never expose the service token in browsers."""
import asyncio
import hashlib
import hmac
import io
import os
import re
import time
from uuid import UUID, uuid4
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_service.services.video.jobs import STAGES, connection
from ai_service.services.video.storage import VolumeResults, VolumeSettings

Owner = Annotated[int, Header(alias='X-Owner-ID', gt=0, le=9223372036854775807)]
PLAYBACK_SECONDS = 300

class JobInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    travel_plan_id: int = Field(gt=0, le=9223372036854775807, strict=True)
    upload_ids: list[UUID] = Field(min_length=2, max_length=120)
    duration_seconds: int = Field(default=30, ge=5, le=30)
    memory_context: str = Field(default='', max_length=500)
    memory_style: Literal['WARM', 'LIVELY', 'CINEMATIC'] = 'WARM'
    music: Literal['AUTO', 'WARM', 'BRIGHT', 'CALM', 'NONE'] = 'AUTO'


def store():
    return VolumeResults(VolumeSettings.from_env())


def find_job(job_id, owner):
    with connection() as conn:
        row = conn.execute('SELECT * FROM video_jobs WHERE id=%s AND owner_id=%s', (job_id, owner)).fetchone()
    if not row:
        raise HTTPException(404, 'Job not found')
    return row


def worker_state():
    with connection() as conn:
        worker = conn.execute("SELECT state, heartbeat_at > now() - interval '30 seconds' AS alive FROM video_worker WHERE id=1").fetchone()
    return worker['state'] if worker and worker['alive'] else 'OFFLINE'


def save_upload(data, owner):
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as photo:
            if photo.format not in {'JPEG', 'PNG', 'WEBP'} or photo.width * photo.height > 40_000_000:
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

    # Declared so the /docs page offers a file picker for the raw image body.
    @routes.post('/api/ai/v1/uploads', status_code=201, openapi_extra={'requestBody': {'required': True, 'content': {
        kind: {'schema': {'type': 'string', 'format': 'binary'}} for kind in ('image/jpeg', 'image/png', 'image/webp')}}})
    async def upload(request: Request, x_owner_id: Owner):
        # Raw image body avoids multipart buffering and requires no multipart dependency.
        data = bytearray()
        try:
            async with asyncio.timeout(120):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > 20 * 1024 * 1024:
                        raise HTTPException(413, 'Maximum image size is 20 MiB')
                    data.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, 'Upload timed out') from None
        return await asyncio.to_thread(save_upload, bytes(data), x_owner_id)

    @routes.post('/api/ai/v1/video-jobs', status_code=202)
    def create(body: JobInput, x_owner_id: Owner):
        from psycopg.types.json import Jsonb
        if len(set(body.upload_ids)) != len(body.upload_ids):
            raise HTTPException(400, 'Duplicate upload IDs')
        payload = body.model_dump(mode='json')
        with connection() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (x_owner_id,))
            existing = conn.execute('SELECT * FROM video_jobs WHERE id=%s', (body.request_id,)).fetchone()
            if existing is None:
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

    @routes.get('/api/ai/v1/video-jobs')
    def list_jobs(x_owner_id: Owner, offset: int = 0):
        if offset < 0:
            raise HTTPException(400, 'Invalid offset')
        with connection() as conn:
            rows = conn.execute('SELECT id,status,stage,error_code,created_at,updated_at FROM video_jobs WHERE owner_id=%s ORDER BY created_at DESC LIMIT 50 OFFSET %s', (x_owner_id, offset)).fetchall()
        return {'items': rows, 'offset': offset, 'limit': 50}

    @routes.get('/api/ai/v1/video-jobs/{job_id}')
    def status(job_id: UUID, x_owner_id: Owner):
        row = find_job(job_id, x_owner_id)
        return {'job_id': str(job_id), 'status': row['status'], 'stage': row['stage'],
                'stage_label': STAGES.get(row['stage'], row['stage']),
                'travel_plan_id': row.get('travel_plan_id'),
                'worker_state': worker_state(),
                'error_code': row['error_code'], 'error_message': row.get('error_message'),
                'created_at': row.get('created_at'), 'completed_at': row.get('completed_at'),
                'updated_at': row['updated_at'],
                'video_url': f'/api/ai/v1/video-jobs/{job_id}/video' if row['status'] == 'COMPLETED' else None}

    @routes.get('/api/ai/v1/video-jobs/{job_id}/video')
    def video(job_id: UUID, x_owner_id: Owner, request: Request,
              response_format: Literal['redirect', 'json'] = 'redirect'):
        row = find_job(job_id, x_owner_id)
        if row['status'] != 'COMPLETED':
            raise HTTPException(409, 'Video is not completed')
        # RunPod's S3 gateway rejects presigned URLs, so the service signs its own short-lived URL.
        expires = int(time.time()) + PLAYBACK_SECONDS
        base = os.environ.get('VIDEO_PUBLIC_BASE_URL') or str(request.base_url)
        url = f"{base.rstrip('/')}/api/ai/v1/playback/{job_id}?exp={expires}&sig={playback_signature(job_id, expires)}"
        if response_format == 'json':
            return JSONResponse({'url': url, 'expires_in': PLAYBACK_SECONDS}, headers={'Cache-Control': 'no-store'})
        return RedirectResponse(url, status_code=307, headers={'Cache-Control': 'no-store'})

    return routes


def playback_signature(job_id, expires):
    secret = os.environ.get('VIDEO_PLAYBACK_SECRET') or os.environ['AUDIGO_API_TOKEN']
    key = hashlib.sha256(b'audigo-playback:' + secret.encode()).digest()
    return hmac.new(key, f'{job_id}:{expires}'.encode(), hashlib.sha256).hexdigest()


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

    @routes.get('/api/ai/v1/playback/{job_id}', include_in_schema=False)
    def playback(job_id: UUID, exp: int, sig: str, range: Annotated[str | None, Header()] = None):
        if exp < time.time() or not hmac.compare_digest(sig, playback_signature(job_id, exp)):
            raise HTTPException(403, 'Playback link expired or invalid')
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
