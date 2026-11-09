"""영상 작업을 순서대로 가져와 GPU를 배정하고, 생성과 결과 저장을 실행한다."""
import asyncio
from dataclasses import replace
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

import httpx
from urllib.parse import urlsplit

from ai_service.services.video.errors import VideoError
from ai_service.services.video.gpu_power import AUTO_GPU, default_gpu_type, gpu_selection_allowed, selectable_gpu_types
from ai_service.services.video.jobs import claim, connection, failure_message, mark_cancelled, recover_interrupted, set_detail, update
from ai_service.services.video.storage import VolumeResults, VolumeSettings
from ai_service.services.video.config import VideoSettings
from ai_service.schemas.video import MemoryVideoRequest, MusicSelection
from ai_service.services.video.pipeline import VideoPipeline
from scripts.video.storage import publish_video

PROGRESS = ['ANALYZING_PHOTOS', 'GENERATING_VIDEO', 'EDITING_VIDEO']
GENERATING_EVENTS = {'generating_scenes', 'discovering', 'uploading', 'generating', 'submitted', 'downloading',
                     'photo_motion_rendering', 'recovering_unused_photos'}
EDITING_EVENTS = {'encoding', 'encoded'}


def stage_from_reports(workspace: Path):
    """Map the pipeline's own report.json events to the user-facing stage; never moves backwards."""
    best = 0
    for report in workspace.glob('*/report.json'):
        try:
            events = json.loads(report.read_text()).get('events', [])
        except (OSError, ValueError):
            continue
        for item in events:
            name = item.get('stage', '')
            if name in EDITING_EVENTS:
                best = max(best, 2)
            elif name in GENERATING_EVENTS or name.startswith(('scene_', 'comfyui_')):
                best = max(best, 1)
        # The film backend renders into these folders without emitting a separate event.
        if (report.parent / 'film').exists() or any(report.parent.glob('verified-film*')):
            best = 2
    return PROGRESS[best]


def progress_detail(workspace: Path, photo_count: int):
    """What the pipeline is doing inside its stage, read from the files and report.json it writes."""
    runs = [path for path in workspace.iterdir() if path.is_dir()] if workspace.is_dir() else []
    if not runs:
        return '사진을 작업 폴더로 옮기는 중'
    run = max(runs, key=lambda path: path.stat().st_mtime)
    try:
        report = json.loads((run / 'report.json').read_text())
    except (OSError, ValueError):
        done = len(list(run.glob('photo-*.png')))
        return f'OpenCV로 사진 품질 검사 중 (흐림·노출·해상도, {done}/{photo_count}장)'
    events = [item.get('stage') for item in report.get('events', [])]
    last = events[-1] if events else ''
    if last == 'planning':
        selected = sum(row.get('decision') == 'selected' for row in report.get('photo_screening', []))
        return f'OpenAI가 사진 내용을 보고 장면을 구성하는 중 (통과한 사진 {selected}/{photo_count}장)'
    if last == 'planned':
        attempts = len(list((run / 'score').glob('attempt-*'))) if (run / 'score').is_dir() else 0
        if attempts:
            return f'GPU에서 배경음악 작곡 중 ({attempts}번째 시도)'
        return 'GPU 연결과 모델 확인 중'
    if last in ('encoding', 'completed') or (run / 'film').exists() or any(run.glob('verified-film*')):
        return 'ffmpeg로 장면과 음악을 합쳐 최종 영상을 만드는 중'
    if last == 'recovering_unused_photos':
        return '탈락한 장면을 남은 사진으로 다시 채우는 중'
    clips = report.get('clips', [])
    total = report.get('planned_scene_count') or 0
    accepted = sum(clip.get('status') == 'accepted' for clip in clips)
    rejected = sum(clip.get('status') == 'rejected' for clip in clips)
    scenes = sorted(run.glob('scene-*-attempt-*'), key=lambda path: path.stat().st_mtime)
    # A scene folder without quality.json is still being generated or reviewed; two GPUs work on two at once.
    working = [path for path in scenes if not (path / 'quality.json').exists()] or scenes[-1:]
    if working:
        parts = []
        for path in working:
            _, slot, _, attempt = path.name.split('-')
            number = int(slot) + 1
            # Slots past the plan are replacements made from unused photos after rejections.
            label = f'{number}/{total}' if number <= total else f'대체 장면 {number - total}'
            parts.append(f'{label} ({int(attempt) + 1}번째 시도)')
        where = 'GPU 2대에서 장면 ' if len(parts) > 1 else 'GPU에서 장면 '
        return f'{where}{", ".join(parts)} 만드는 중 · 통과 {accepted}개, 탈락 {rejected}개'

    return 'GPU에서 장면 생성을 시작하는 중'


SOLD_OUT_RECYCLE_SECONDS = 120  # a worker stuck "throttled" this long is replaced (2026-11-09: one stayed 30 min)


async def track_progress(job_id, workspace: Path, photo_count=0, settings=None):
    current, detail = PROGRESS[0], None
    settings = settings or VideoSettings.from_env()
    gpu_state, checked = 'STARTING_GPU', -1e9
    sold_out_since, recycles = None, 0
    while True:
        await asyncio.sleep(2)
        stage = stage_from_reports(workspace)
        if PROGRESS.index(stage) > PROGRESS.index(current):
            await asyncio.to_thread(update, job_id, stage)
            current = stage
        try:
            text = progress_detail(workspace, photo_count)
        except OSError:
            continue
        if text == 'GPU 연결과 모델 확인 중' and settings.endpoint_mode == 'serverless':
            now = asyncio.get_running_loop().time()
            if now - checked > 15:
                gpu_state, checked = await serverless_state(settings), now
                sold_out_since = (sold_out_since or now) if gpu_state == 'GPU_SOLD_OUT' else None
                # RunPod keeps a throttled worker waiting for a GPU on its first machine; dropping it lets the
                # endpoint schedule a fresh worker wherever an allowed GPU is free.
                if sold_out_since is not None and now - sold_out_since >= SOLD_OUT_RECYCLE_SECONDS:
                    recycles += 1
                    logging.info('Job %s: GPU sold out for %ds; asking RunPod for another GPU (%d)',
                                 job_id, int(now - sold_out_since), recycles)
                    await recycle_endpoint(settings)
                    sold_out_since = None
            text = (f"GPU 품절: 다른 빈 GPU를 찾는 중 ({recycles}번 다시 찾음, 사진 분석은 끝남)" if gpu_state == 'GPU_SOLD_OUT' and recycles
                    else 'GPU 품절: 빈 GPU가 생기기를 기다리는 중 (사진 분석은 끝남)' if gpu_state == 'GPU_SOLD_OUT'
                    else 'GPU가 켜지기를 기다리는 중 (사진 분석은 끝남, 켜는 데 약 4~5분)')
        if text != detail:
            await asyncio.to_thread(set_detail, job_id, text)
            detail = text


async def allow_one_worker(settings):
    """Put max workers back to 1; a job killed in the middle of recycle_endpoint could leave it at 0."""
    endpoint_id = urlsplit(settings.comfyui_url).hostname.split('.')[0]
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            (await http.patch(f'https://rest.runpod.io/v1/endpoints/{endpoint_id}', json={'workersMax': 1},
                              headers={'Authorization': f'Bearer {settings.runpod_api_key}'})).raise_for_status()
    except httpx.HTTPError as exc:
        logging.warning('Could not set max workers to 1 on %s (%s)', endpoint_id, type(exc).__name__)


async def recycle_endpoint(settings, http=None, wait_seconds=60):
    """Drop the endpoint's waiting worker (max workers 0) and allow one again (max 1). Best effort: any error
    leaves the endpoint at max 1 so the job keeps waiting as before."""
    endpoint_id = urlsplit(settings.comfyui_url).hostname.split('.')[0]
    url = f'https://rest.runpod.io/v1/endpoints/{endpoint_id}'
    headers = {'Authorization': f'Bearer {settings.runpod_api_key}'}
    loop = asyncio.get_running_loop()
    own = http is None
    http = http or httpx.AsyncClient(timeout=20)
    try:
        (await http.patch(url, json={'workersMax': 0}, headers=headers)).raise_for_status()
        deadline = loop.time() + wait_seconds
        while loop.time() < deadline:
            response = await http.get(f'https://api.runpod.ai/v2/{endpoint_id}/health', headers=headers)
            if not sum(response.json().get('workers', {}).values()):
                break
            await asyncio.sleep(5)
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logging.warning('GPU recycle: could not drop the waiting worker (%s)', type(exc).__name__)
    finally:
        for attempt in range(3):
            try:
                (await http.patch(url, json={'workersMax': 1}, headers=headers)).raise_for_status()
                break
            except httpx.HTTPError as exc:
                logging.warning('GPU recycle: max workers back to 1 failed (%s), retrying', type(exc).__name__)
                await asyncio.sleep(5)
        if own:
            await http.aclose()


# User photos and generated media; text reports stay for debugging.
MEDIA_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.image', '.mp4', '.mov', '.webm', '.flac', '.wav', '.mp3',
                  '.m4a', '.aac', '.partial', '.tmp'}


def purge_media(folder: Path):
    """Delete photo copies, clips, audio and renders under a job workspace; keep JSON reports."""
    removed = 0
    for path in sorted(folder.rglob('*'), reverse=True):
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
            path.unlink()
            removed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


def gpu_endpoints(settings):
    """Settings for every GPU endpoint this job may use: COMFYUI_URL plus COMFYUI_EXTRA_URLS."""
    return [settings, *[replace(settings, comfyui_url=url, comfyui_extra_urls=()) for url in settings.comfyui_extra_urls]]


async def gpu_interrupt(settings):
    """Stop whatever the GPU is generating and empty its queue. Each GPU Pod serves only this worker, so
    ComfyUI's global interrupt cannot touch anyone else's work."""
    from ai_service.clients.runpod import EndpointResolver
    for each in gpu_endpoints(settings):
        if each.endpoint_mode == 'serverless' and not await gpu_ready_now(each):
            continue  # no running GPU: nothing to stop, and a request would boot one
        headers = each.gpu_headers()
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                endpoint = await EndpointResolver(http, each).resolve()
                await http.post(endpoint.url + '/queue', json={'clear': True}, headers=headers)
                await http.post(endpoint.url + '/interrupt', headers=headers)
        except Exception as exc:
            logging.warning('GPU interrupt skipped (%s)', type(exc).__name__)


def cancel_requested(job_id):
    with connection() as conn:
        row = conn.execute('SELECT cancel_requested FROM video_jobs WHERE id=%s', (job_id,)).fetchone()
    return bool(row and row['cancel_requested'])


async def gpu_cleanup(settings):
    """Ask the GPU's password gate to delete this service's ComfyUI inputs and outputs (best effort)."""
    from ai_service.clients.runpod import EndpointResolver
    for each in gpu_endpoints(settings):
        if each.endpoint_mode == 'serverless' and not await gpu_ready_now(each):
            continue  # a GPU that is off or still booting has none of this job's files yet
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                endpoint = await EndpointResolver(http, each).resolve()
                response = await http.post(endpoint.url + '/_audigo/cleanup', headers=each.gpu_headers())
                response.raise_for_status()
                logging.info('GPU cleanup removed %s file(s)', response.json().get('removed_files'))
        except Exception as exc:
            logging.warning('GPU cleanup skipped (%s)', type(exc).__name__)


async def run_job(row):
    job_id, payload = row['id'], row['request']
    volume = VolumeResults(VolumeSettings.from_env())
    with connection() as conn:
        rows = conn.execute('SELECT id,storage_key FROM video_uploads WHERE owner_id=%s AND id=ANY(%s::uuid[])',
                            (row['owner_id'], payload['upload_ids'])).fetchall()
    keys = {str(photo['id']): photo['storage_key'] for photo in rows}
    with tempfile.TemporaryDirectory(prefix='audigo-video-') as folder:
        photos = []
        set_detail(job_id, f"저장소에서 사진 {len(payload['upload_ids'])}장을 내려받는 중")
        for index, upload_id in enumerate(payload['upload_ids']):
            path = Path(folder) / f'{index:04d}.png'
            await asyncio.to_thread(volume.client.download_file, volume.settings.volume_id, keys[upload_id], str(path))
            photos.append(path)
        # A persistent workspace per job keeps failure reports and lets progress tracking read only this job.
        base = VideoSettings.from_env()
        workspace = base.workspace / f'job-{job_id}-attempt-{row["attempts"]}'
        workspace.mkdir(parents=True, exist_ok=True)
        settings = replace(base, generation_profile='fast', workspace=workspace, music_preset_fallback=True)
        tracker = asyncio.create_task(track_progress(job_id, workspace, len(photos), settings))
        try:
            await generate_and_publish(job_id, payload, photos, settings)
        finally:
            tracker.cancel()
            # Success or failure, no copy of the user's photos or video stays on the CPU work disk.
            logging.info('Job %s: removed %d local media file(s)', job_id, purge_media(workspace))


MUSIC_FALLBACK_NOTICE = '자동 작곡 대신 여행 분위기에 맞는 기본 배경음악을 넣었습니다.'


PHOTO_MOTION_NOTICE = 'AI 영상 생성이 원활하지 않아, 사진에 움직임을 넣은 영상으로 대신 만들었습니다.'
PHOTO_MOTION_MAX_PHOTOS = 10


async def generate_and_publish(job_id, payload, photos, settings):
    """배경음악이 들어간 최종 영상을 저장하고 작업을 완료한다. AI 영상이 어떤 이유로든 안 되면(GPU 품절,
    품질 검사, 장면 부족, 모델 오류) GPU 없이 사진에 움직임을 넣은 영상으로 대신 완성한다."""
    preset = payload.get('music', 'AUTO')
    # 배포 전에 대기열에 들어온 무음 요청도 배경음악을 포함해 처리한다.
    if preset == 'NONE':
        preset = 'AUTO'
    request = MemoryVideoRequest(photos=photos, duration_seconds=payload['duration_seconds'],
                                 memory_context=payload['memory_context'], memory_style=payload['memory_style'],
                                 music=MusicSelection(preset=preset), seed=job_seed(job_id))
    try:
        async with httpx.AsyncClient(follow_redirects=False) as http:
            result = await VideoPipeline(http, settings, execution_mode='live').generate(request)
        if result.get('status') != 'completed' or not result.get('output_file'):
            raise RuntimeError(result.get('failure_reason') or 'Pipeline did not complete')
        if result['media'].get('audio_streams') != 1:
            raise VideoError('final_video_music_missing')
        notice = MUSIC_FALLBACK_NOTICE if result['music'].get('source') == 'BUILTIN_PRESET_FALLBACK' else None
    except Exception as exc:
        reason = getattr(exc, 'reason', None) or type(exc).__name__
        logging.warning('Job %s: AI video failed (%s); making the photo-motion film instead', job_id, reason)
        set_detail(job_id, 'AI 영상 생성이 원활하지 않아 사진에 움직임을 넣은 영상으로 만드는 중')
        result = await photo_motion_film(payload, photos, settings, preset, job_seed(job_id))
        notice = PHOTO_MOTION_NOTICE
    update(job_id, 'SAVING')
    publication = await publish_video(Path(result['output_file']))
    update(job_id, 'COMPLETED', status='COMPLETED', publication=publication, notice=notice)


def readable_photo(path):
    """One unreadable file must not stop the last-resort film; it is simply left out."""
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


async def photo_motion_film(payload, photos, settings, preset, seed):
    """GPU 없이 CPU(ffmpeg)로 만드는 마지막 대안: 고르게 뽑은 사진 최대 10장에 천천히 움직이는 효과와 자체 합성
    배경음악을 넣는다. 사진을 읽을 수만 있으면 항상 끝난다."""
    from ai_service.schemas.video import VideoRequest
    readable = await asyncio.to_thread(lambda: [path for path in photos if readable_photo(path)])
    if not readable:
        raise VideoError('photo_motion_no_readable_photos')
    step = max(1, len(readable) / PHOTO_MOTION_MAX_PHOTOS)
    chosen = [readable[int(i * step)] for i in range(min(len(readable), PHOTO_MOTION_MAX_PHOTOS))]
    if len(chosen) < 2:
        chosen = chosen * 2
    request = VideoRequest(photos=chosen, duration_seconds=max(10, payload['duration_seconds']), story_mode='PHOTO_MOTION',
                           memory_context=payload['memory_context'], memory_style=payload['memory_style'],
                           music=MusicSelection(preset=preset), seed=seed % (2**63))
    local = replace(settings, workspace=settings.workspace / 'photo-motion')
    async with httpx.AsyncClient(follow_redirects=False) as http:
        result = await VideoPipeline(http, local, execution_mode='live').generate(request)
    if result.get('status') != 'completed' or not result.get('output_file'):
        raise VideoError('photo_motion_failed')
    return result


def job_seed(job_id):
    """작업 ID마다 서로 다른 시드를 사용해 작곡 결과를 바꾼다."""
    return int(hashlib.sha256(str(job_id).encode()).hexdigest()[:12], 16)


def required_models(settings):
    """(ComfyUI loader node, input, file) for every model the default film and music path loads."""
    from ai_service.services.video.ace_music import ace_music_downloads
    loaders = {'diffusion_models': ('UNETLoader', 'unet_name'), 'text_encoders': ('DualCLIPLoader', 'clip_name1'),
               'vae': ('VAELoader', 'vae_name')}
    return [('CheckpointLoaderSimple', 'ckpt_name', settings.ltx_checkpoint),
            ('LTXAVTextEncoderLoader', 'text_encoder', settings.ltx_text_encoder),
            *[(*loaders[item['directory']], item['filename']) for item in ace_music_downloads('turbo')]]


def missing_models(object_info, required):
    """Files a loader cannot offer. ComfyUI lists choices as [[choices], {...}] or, for newer nodes,
    ['COMBO', {'options': [choices]}]."""
    missing = []
    for node, field, filename in required:
        spec = (object_info.get(node) or {}).get('input', {}).get('required', {}).get(field)
        choices = []
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            choices = spec[0]
        elif isinstance(spec, list) and len(spec) > 1 and spec[0] == 'COMBO' and isinstance(spec[1], dict):
            choices = spec[1].get('options') or []
        if filename not in choices:
            missing.append(filename)
    return missing


async def gpu_check(settings):
    """READY, NO_GPU (no ComfyUI Pod answers), MODELS_MISSING or GPU_BUSY. Never claims a job on a GPU that
    lacks its models, so a freshly created empty Pod cannot fail queued jobs."""
    from ai_service.clients.runpod import EndpointResolver
    from ai_service.clients.comfyui import ComfyUI
    try:
        async with httpx.AsyncClient() as http:
            endpoint = await EndpointResolver(http, settings).resolve()
            client = ComfyUI(http, endpoint, settings)
            await client.runtime_info(require_cuda=True)
            required = required_models(settings)
            info = {}
            for node in {node for node, _, _ in required}:
                info.update(await client._json('GET', f'/object_info/{node}'))
            if missing := missing_models(info, required):
                logging.warning('GPU Pod is up but lacks %d model file(s): %s', len(missing), ', '.join(missing))
                return 'MODELS_MISSING', None
            queue = await client._json('GET', '/queue')
            if queue.get('queue_running') or queue.get('queue_pending'):
                return 'GPU_BUSY', None
            return 'READY', endpoint
    except Exception as exc:
        logging.info('No usable GPU yet (%s: %s)', type(exc).__name__, getattr(exc, 'reason', '') or str(exc)[:160])
        return 'NO_GPU', None


async def serverless_state(settings):
    """GPU_SOLD_OUT when RunPod has no free GPU of any allowed type for the endpoint, else STARTING_GPU."""
    endpoint_id = urlsplit(settings.comfyui_url).hostname.split('.')[0]
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            response = await http.get(f'https://api.runpod.ai/v2/{endpoint_id}/health',
                                      headers={'Authorization': f'Bearer {settings.runpod_api_key}'})
            workers = response.json().get('workers', {})
    except (httpx.HTTPError, ValueError, AttributeError):
        return 'STARTING_GPU'
    starting = workers.get('initializing', 0) + workers.get('running', 0) + workers.get('ready', 0)
    return 'GPU_SOLD_OUT' if workers.get('throttled') and not starting else 'STARTING_GPU'


def endpoint_id(settings):
    return urlsplit(settings.comfyui_url).hostname.split('.')[0]


async def gpu_workers(settings, http):
    """Worker counts from RunPod's health API, which never starts a worker (unlike a request to the endpoint)."""
    response = await http.get(f'https://api.runpod.ai/v2/{endpoint_id(settings)}/health',
                              headers={'Authorization': f'Bearer {settings.runpod_api_key}'})
    return response.json().get('workers', {})


async def gpu_ready_now(settings):
    """True only when a serverless worker exists and ComfyUI answers. A request to a load-balancing endpoint
    without a worker queues and boots a GPU (2026-11-09: cancelled jobs' cleanup calls woke ten GPUs), so the
    health API is asked first and the endpoint is touched only when a worker is already there."""
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            workers = await gpu_workers(settings, http)
            if not any(workers.get(state) for state in ('idle', 'ready', 'running')):
                return False
            from ai_service.clients.runpod import EndpointResolver, GpuEndpoint
            return await EndpointResolver(http, settings)._ready(GpuEndpoint(settings.comfyui_url.rstrip('/')))
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


async def drop_worker(settings, wait_seconds=60):
    """Stop the endpoint's GPU now (max workers 0) instead of letting it finish booting and idle; the caller
    restores max workers 1 with allow_one_worker."""
    url = f'https://rest.runpod.io/v1/endpoints/{endpoint_id(settings)}'
    loop = asyncio.get_running_loop()
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            (await http.patch(url, json={'workersMax': 0},
                              headers={'Authorization': f'Bearer {settings.runpod_api_key}'})).raise_for_status()
            deadline = loop.time() + wait_seconds
            while loop.time() < deadline and sum((await gpu_workers(settings, http)).values()):
                await asyncio.sleep(5)
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logging.warning('Could not stop the GPU on %s (%s)', endpoint_id(settings), type(exc).__name__)


# A cold start takes 4-6 minutes. After 15 minutes without a GPU the job stops waiting and makes the
# photo-motion film on the CPU instead, so the whole job stays well under 30 minutes.
GPU_WAIT_SECONDS = 900


async def warm_gpu(settings):
    """Ask the serverless endpoint for ComfyUI until it answers, so RunPod boots the GPU while the CPU analyses
    the photos. Only readiness requests are sent; nothing is submitted to the GPU here."""
    from ai_service.clients.runpod import EndpointResolver
    loop = asyncio.get_running_loop()
    deadline = loop.time() + GPU_WAIT_SECONDS
    async with httpx.AsyncClient() as http:
        resolver = EndpointResolver(http, settings)
        while loop.time() < deadline:
            from ai_service.clients.runpod import GpuEndpoint
            if await resolver._ready(GpuEndpoint(settings.comfyui_url.rstrip('/'))):
                logging.info('Serverless GPU is ready')
                return
            await asyncio.sleep(10)


async def with_heartbeat(conn, state, awaitable):
    """Keep the worker's heartbeat fresh while a slow call runs: a cold serverless GPU can hold a readiness
    request for two minutes, which used to show the worker as not responding."""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        heartbeat(conn, state)
        await asyncio.wait({task}, timeout=10)
    return task.result()


def heartbeat(conn, state):
    conn.execute("INSERT INTO video_worker(id,state) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET state=EXCLUDED.state,heartbeat_at=now()", (state,))


async def supervise(lease, row, job_settings, process, warmup, power=None, beat=True):
    """Wait for one job's child process; on cancel, timeout or shutdown stop it and clean its GPUs up."""
    import os, signal
    cancelled = False
    try:
        async with asyncio.timeout(7200):
            while process.returncode is None:
                if beat:
                    heartbeat(lease, 'BUSY')
                if power:
                    power.mark_busy()
                if cancel_requested(row['id']):
                    cancelled = True
                    break
                await asyncio.sleep(2)
        if cancelled:
            logging.info('Job %s: cancel requested; stopping generation', row['id'])
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            await gpu_interrupt(job_settings)
            with connection() as conn:
                mark_cancelled(conn, row['id'])
            # The killed child cannot run its own cleanup, so remove its local copies here.
            workspace = VideoSettings.from_env().workspace / f"job-{row['id']}-attempt-{row['attempts']}"
            if workspace.is_dir():
                purge_media(workspace)
        with connection() as conn:
            conn.execute("""UPDATE video_jobs SET status='FAILED',stage='FAILED',error_code='WORKER_PROCESS_FAILED',
                            error_message='영상 생성 프로세스가 비정상 종료되었습니다.',completed_at=now(),updated_at=now()
                            WHERE id=%s AND status='RUNNING'""", (row['id'],))
    finally:
        if warmup:
            warmup.cancel()
        # Lease loss / shutdown / timeout kills the whole child process group, including encoders.
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        # Photos and clips uploaded to the GPU stay on its persistent volume unless removed.
        await gpu_cleanup(job_settings)


# One GPU per video is the cheapest: every GPU pays its own 3-4 minute boot and idle minute, so a second GPU
# made a film ~2-3 minutes faster for ~60% more GPU cost (2026-11-09). VIDEO_MAX_GPUS_PER_JOB=2 restores it.
MAX_GPUS_PER_JOB = max(1, int(os.environ.get('VIDEO_MAX_GPUS_PER_JOB', '1')))


def gpu_allocation(free, waiting):
    """GPUs for the next job: two scene lanes (faster) while that still leaves a GPU for every other waiting
    job, else one, so up to one job per GPU runs at once when many people ask together."""
    if free <= 0 or waiting <= 0:
        return 0
    return MAX_GPUS_PER_JOB if free >= MAX_GPUS_PER_JOB and free - MAX_GPUS_PER_JOB >= waiting - 1 else 1


def job_env(urls):
    """Environment that pins a child job to its own serverless endpoints (one worker each)."""
    import os
    env = os.environ.copy()
    env['COMFYUI_URL'], env['COMFYUI_EXTRA_URLS'] = urls[0], ','.join(urls[1:])
    env.setdefault('VIDEO_GPU_WAIT_SECONDS', str(GPU_WAIT_SECONDS))
    return env


async def run_on_gpus(lease, base, row, urls):
    import sys
    job_settings = replace(base, comfyui_url=urls[0], comfyui_extra_urls=tuple(urls[1:]))
    # Boot this job's GPUs while the CPU analyses its photos.
    warmup = asyncio.gather(*[warm_gpu(each) for each in gpu_endpoints(job_settings)])
    logging.info('Job %s: %d GPU endpoint(s) %s', row['id'], len(urls), ', '.join(urls))
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'scripts.video.worker', '--job', str(row['id']),
                                                   env=job_env(urls), start_new_session=True)
    try:
        await supervise(lease, row, job_settings, process, warmup, beat=False)
    finally:
        async def release(each):
            # A job that failed or was cancelled before its GPU was ready would otherwise pay for the whole
            # boot plus the idle minute. A ready GPU stays on for its idle minute so the next job can reuse it.
            if not await gpu_ready_now(each):
                await drop_worker(each)
            await allow_one_worker(each)
        await asyncio.gather(*[release(each) for each in gpu_endpoints(job_settings)])


async def serve_parallel(lease, base):
    """Serverless mode: each endpoint has at most one GPU worker, which keeps a job's ComfyUI files, so every
    running job gets endpoints of its own. Jobs run at once with one or two GPUs each, up to one per endpoint."""
    urls = [base.comfyui_url, *base.comfyui_extra_urls]
    busy, released, tasks = {}, {}, set()
    await asyncio.gather(*[allow_one_worker(each) for each in gpu_endpoints(base)])
    loop = asyncio.get_running_loop()

    def finished(task, chosen):
        for url in chosen:
            busy.pop(url, None)
            released[url] = loop.time()
        tasks.discard(task)
        if not task.cancelled() and task.exception():
            logging.error('Job supervisor failed: %r', task.exception())

    while True:
        waiting = lease.execute("SELECT id FROM video_jobs WHERE status='QUEUED' ORDER BY created_at,id").fetchall()
        # Most recently released first: its GPU may still be warm (idle timeout), so the job skips the boot.
        free = sorted((url for url in urls if url not in busy), key=lambda url: released.get(url, 0), reverse=True)
        count = gpu_allocation(len(free), len(waiting))
        if count:
            row = claim(lease, waiting[0]['id'], f'서버리스 GPU {count}대')
            if row:
                chosen = free[:count]
                for url in chosen:
                    busy[url] = row['id']
                task = asyncio.create_task(run_on_gpus(lease, base, row, chosen))
                tasks.add(task)
                task.add_done_callback(lambda done, chosen=chosen: finished(done, chosen))
            continue
        heartbeat(lease, 'ALL_GPUS_BUSY' if waiting else 'BUSY' if busy else 'IDLE')
        await asyncio.sleep(2)


async def main():
    import os, signal, sys
    from ai_service.services.video.gpu_power import GpuPower, PowerSettings
    logging.basicConfig(level=logging.INFO)
    power_settings = PowerSettings.from_env()
    power = GpuPower(power_settings) if power_settings else None
    base = VideoSettings.from_env()
    allowed_gpus = selectable_gpu_types(base)
    serverless = base.endpoint_mode == 'serverless'
    if power and serverless:
        raise SystemExit('VIDEO_GPU_AUTOSTART creates GPU Pods; turn it off when VIDEO_ENDPOINT_MODE=serverless')
    with connection() as lease:
        lease.autocommit = True
        if not lease.execute('SELECT pg_try_advisory_lock(41420261104) AS acquired').fetchone()['acquired']:
            raise SystemExit('Another video worker already owns this queue')
        # A job left RUNNING by a crash or restart goes back to the queue instead of being lost.
        recover_interrupted(lease)
        if serverless:
            await serve_parallel(lease, base)
            return
        while True:
            heartbeat(lease, 'IDLE')
            pending = lease.execute("SELECT id,request FROM video_jobs WHERE status='QUEUED' ORDER BY created_at,id LIMIT 1").fetchone()
            if not pending:
                if power:
                    await power.stop_if_idle()
                await asyncio.sleep(3)
                continue
            if power:
                power.mark_busy()
            # A serverless endpoint has one fixed GPU pool, so a GPU picked for the job is ignored there.
            requested_gpu = None if serverless else pending['request'].get('gpu_type')
            if requested_gpu is not None and not gpu_selection_allowed(requested_gpu, allowed_gpus):
                failed = claim(lease, pending['id'])
                if failed:
                    reason = 'video_gpu_not_selectable'
                    update(failed['id'], 'FAILED', status='FAILED', error=reason, message=failure_message(reason))
                continue
            gpu_type = requested_gpu or default_gpu_type(allowed_gpus)
            candidates = allowed_gpus if gpu_type == AUTO_GPU else (gpu_type,) if gpu_type else ()
            job_settings = replace(base, runpod_gpu_types=candidates)
            heartbeat(lease, 'CHECKING_GPU')
            warmup = None
            if serverless:
                # Start the job now: photo analysis runs on this CPU server while RunPod boots the GPU, and the
                # job's first GPU call waits for it (VIDEO_GPU_WAIT_SECONDS). The GPU starts at the same moment
                # as before, so this only removes the minutes it used to sit idle during analysis.
                gpu, endpoint = 'READY', None
                warmup = asyncio.gather(*[warm_gpu(each) for each in gpu_endpoints(job_settings)])
            else:
                gpu, endpoint = await with_heartbeat(lease, 'CHECKING_GPU', gpu_check(job_settings))
            if gpu != 'READY':
                if gpu == 'NO_GPU' and power and allowed_gpus:
                    await power.ensure_started(gpu_type)
                    heartbeat(lease, 'WAITING_FOR_GPU_CAPACITY' if gpu_type in power.capacity_waiting else 'STARTING_GPU')
                elif gpu == 'NO_GPU' and serverless:
                    # The readiness request itself makes RunPod start a serverless worker.
                    heartbeat(lease, await serverless_state(job_settings))
                else:
                    heartbeat(lease, {'NO_GPU': 'WAITING_FOR_GPU', 'MODELS_MISSING': 'GPU_MODELS_MISSING',
                                      'GPU_BUSY': 'GPU_BUSY'}[gpu])
                await asyncio.sleep(10)
                continue
            row = claim(lease, pending['id'], endpoint.gpu_type if endpoint else None)
            if row is None:
                if warmup:
                    warmup.cancel()
                continue
            # 생성·취소·정리가 준비 상태를 확인한 같은 Pod를 대상으로 하도록 고정한다.
            env = os.environ.copy()
            if serverless:
                env.setdefault('VIDEO_GPU_WAIT_SECONDS', str(GPU_WAIT_SECONDS))
            if endpoint and endpoint.pod_id:
                job_settings = replace(job_settings, runpod_pod_id=endpoint.pod_id)
                env['VIDEO_GPU_POD_ID'] = endpoint.pod_id
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'scripts.video.worker', '--job', str(row['id']),
                                                           env=env, start_new_session=True)
            await supervise(lease, row, job_settings, process, warmup, power)


async def child(job_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM video_jobs WHERE id=%s AND status='RUNNING'", (job_id,)).fetchone()
    if not row:
        raise SystemExit('Job is not running')
    try:
        await run_job(row)
    except Exception as exc:
        reason = getattr(exc, 'reason', None) or type(exc).__name__
        update(row['id'], 'FAILED', status='FAILED', error=reason[:100], message=failure_message(reason))
        logging.error('Video job %s failed (%s %s %s); inspect local pipeline report', row['id'], type(exc).__name__,
                      reason, getattr(exc, 'fields', '') or '')
        raise SystemExit(1)


if __name__ == '__main__':
    import sys
    from uuid import UUID
    if len(sys.argv) == 3 and sys.argv[1] == '--job':
        asyncio.run(child(UUID(sys.argv[2])))
    else:
        asyncio.run(main())
