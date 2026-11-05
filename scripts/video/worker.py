"""영상 작업을 순서대로 가져와 GPU를 배정하고, 생성과 결과 저장을 실행한다."""
import asyncio
from dataclasses import replace
import hashlib
import json
import logging
import tempfile
from pathlib import Path

import httpx

from ai_service.services.video.errors import VideoError
from ai_service.services.video.gpu_power import AUTO_GPU, default_gpu_type, gpu_selection_allowed, selectable_gpu_types
from ai_service.services.video.jobs import claim, connection, failure_message, mark_cancelled, recover_interrupted, update
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
        if (report.parent / 'film').exists() or any(report.parent.glob('verified-film-*')):
            best = 2
    return PROGRESS[best]


async def track_progress(job_id, workspace: Path):
    current = PROGRESS[0]
    while True:
        await asyncio.sleep(2)
        stage = stage_from_reports(workspace)
        if PROGRESS.index(stage) > PROGRESS.index(current):
            await asyncio.to_thread(update, job_id, stage)
            current = stage


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


async def gpu_interrupt(settings):
    """Stop whatever the GPU is generating and empty its queue. Each GPU Pod serves only this worker, so
    ComfyUI's global interrupt cannot touch anyone else's work."""
    from ai_service.clients.runpod import EndpointResolver
    headers = settings.gpu_headers()
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            endpoint = await EndpointResolver(http, settings).resolve()
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
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            endpoint = await EndpointResolver(http, settings).resolve()
            response = await http.post(endpoint.url + '/_audigo/cleanup',
                                       headers=settings.gpu_headers())
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
        for index, upload_id in enumerate(payload['upload_ids']):
            path = Path(folder) / f'{index:04d}.png'
            await asyncio.to_thread(volume.client.download_file, volume.settings.volume_id, keys[upload_id], str(path))
            photos.append(path)
        # A persistent workspace per job keeps failure reports and lets progress tracking read only this job.
        base = VideoSettings.from_env()
        workspace = base.workspace / f'job-{job_id}-attempt-{row["attempts"]}'
        workspace.mkdir(parents=True, exist_ok=True)
        settings = replace(base, generation_profile='fast', workspace=workspace, music_preset_fallback=True)
        tracker = asyncio.create_task(track_progress(job_id, workspace))
        try:
            await generate_and_publish(job_id, payload, photos, settings)
        finally:
            tracker.cancel()
            # Success or failure, no copy of the user's photos or video stays on the CPU work disk.
            logging.info('Job %s: removed %d local media file(s)', job_id, purge_media(workspace))


MUSIC_FALLBACK_NOTICE = '자동 작곡 대신 여행 분위기에 맞는 기본 배경음악을 넣었습니다.'


async def generate_and_publish(job_id, payload, photos, settings):
    """배경음악이 들어간 최종 영상만 저장하고 작업을 완료한다."""
    preset = payload.get('music', 'AUTO')
    # 배포 전에 대기열에 들어온 무음 요청도 배경음악을 포함해 처리한다.
    if preset == 'NONE':
        preset = 'AUTO'
    request = MemoryVideoRequest(photos=photos, duration_seconds=payload['duration_seconds'],
                                 memory_context=payload['memory_context'], memory_style=payload['memory_style'],
                                 music=MusicSelection(preset=preset), seed=job_seed(job_id))
    async with httpx.AsyncClient(follow_redirects=False) as http:
        result = await VideoPipeline(http, settings, execution_mode='live').generate(request)
    if result.get('status') != 'completed' or not result.get('output_file'):
        raise RuntimeError(result.get('failure_reason') or 'Pipeline did not complete')
    if result['media'].get('audio_streams') != 1:
        raise VideoError('final_video_music_missing')
    notice = MUSIC_FALLBACK_NOTICE if result['music'].get('source') == 'BUILTIN_PRESET_FALLBACK' else None
    update(job_id, 'SAVING')
    publication = await publish_video(Path(result['output_file']))
    update(job_id, 'COMPLETED', status='COMPLETED', publication=publication, notice=notice)


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


def heartbeat(conn, state):
    conn.execute("INSERT INTO video_worker(id,state) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET state=EXCLUDED.state,heartbeat_at=now()", (state,))


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
            gpu, endpoint = await gpu_check(job_settings)
            if gpu != 'READY':
                if gpu == 'NO_GPU' and power and allowed_gpus:
                    await power.ensure_started(gpu_type)
                    heartbeat(lease, 'WAITING_FOR_GPU_CAPACITY' if gpu_type in power.capacity_waiting else 'STARTING_GPU')
                elif gpu == 'NO_GPU' and serverless:
                    # The readiness request itself makes RunPod start a serverless worker.
                    heartbeat(lease, 'STARTING_GPU')
                else:
                    heartbeat(lease, {'NO_GPU': 'WAITING_FOR_GPU', 'MODELS_MISSING': 'GPU_MODELS_MISSING',
                                      'GPU_BUSY': 'GPU_BUSY'}[gpu])
                await asyncio.sleep(10)
                continue
            row = claim(lease, pending['id'], endpoint.gpu_type)
            if row is None:
                continue
            # 생성·취소·정리가 준비 상태를 확인한 같은 Pod를 대상으로 하도록 고정한다.
            env = os.environ.copy()
            if endpoint.pod_id:
                job_settings = replace(job_settings, runpod_pod_id=endpoint.pod_id)
                env['VIDEO_GPU_POD_ID'] = endpoint.pod_id
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'scripts.video.worker', '--job', str(row['id']),
                                                           env=env, start_new_session=True)
            cancelled = False
            try:
                async with asyncio.timeout(7200):
                    while process.returncode is None:
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
