"""Sequential PostgreSQL queue worker. One instance owns the configured GPU queue."""
import asyncio
from dataclasses import replace
import json
import logging
import tempfile
from pathlib import Path

import httpx

from ai_service.services.video.jobs import claim, connection, recover_interrupted, update
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
        request = MemoryVideoRequest(photos=photos, duration_seconds=payload['duration_seconds'],
                                     memory_context=payload['memory_context'], memory_style=payload['memory_style'],
                                     music=MusicSelection(preset=payload['music']))
        # A persistent workspace per job keeps failure reports and lets progress tracking read only this job.
        base = VideoSettings.from_env()
        workspace = base.workspace / f'job-{job_id}-attempt-{row["attempts"]}'
        workspace.mkdir(parents=True, exist_ok=True)
        settings = replace(base, generation_profile='fast', workspace=workspace)
        tracker = asyncio.create_task(track_progress(job_id, workspace))
        try:
            async with httpx.AsyncClient(follow_redirects=False) as http:
                result = await VideoPipeline(http, settings, execution_mode='live').generate(request)
        finally:
            tracker.cancel()
        if result.get('status') != 'completed' or not result.get('output_file'):
            raise RuntimeError(result.get('failure_reason') or 'Pipeline did not complete')
        update(job_id, 'SAVING')
        publication = await publish_video(Path(result['output_file']))
        update(job_id, 'COMPLETED', status='COMPLETED', publication=publication)


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


async def gpu_check():
    """READY, NO_GPU (no ComfyUI Pod answers), MODELS_MISSING or GPU_BUSY. Never claims a job on a GPU that
    lacks its models, so a freshly created empty Pod cannot fail queued jobs."""
    from ai_service.clients.runpod import EndpointResolver
    from ai_service.clients.comfyui import ComfyUI
    settings = VideoSettings.from_env()
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
                return 'MODELS_MISSING'
            queue = await client._json('GET', '/queue')
            return 'GPU_BUSY' if queue.get('queue_running') or queue.get('queue_pending') else 'READY'
    except Exception as exc:
        logging.info('No usable GPU yet (%s: %s)', type(exc).__name__, getattr(exc, 'reason', '') or str(exc)[:160])
        return 'NO_GPU'


def heartbeat(conn, state):
    conn.execute("INSERT INTO video_worker(id,state) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET state=EXCLUDED.state,heartbeat_at=now()", (state,))


async def main():
    import os, signal, sys
    from ai_service.services.video.gpu_power import GpuPower, PowerSettings
    logging.basicConfig(level=logging.INFO)
    power_settings = PowerSettings.from_env()
    power = GpuPower(power_settings) if power_settings else None
    with connection() as lease:
        lease.autocommit = True
        if not lease.execute('SELECT pg_try_advisory_lock(41420261104) AS acquired').fetchone()['acquired']:
            raise SystemExit('Another video worker already owns this queue')
        # A job left RUNNING by a crash or restart goes back to the queue instead of being lost.
        recover_interrupted(lease)
        while True:
            heartbeat(lease, 'IDLE')
            pending = lease.execute("SELECT id FROM video_jobs WHERE status='QUEUED' LIMIT 1").fetchone()
            if not pending:
                if power:
                    await power.stop_if_idle()
                await asyncio.sleep(3)
                continue
            if power:
                power.mark_busy()
            heartbeat(lease, 'CHECKING_GPU')
            gpu = await gpu_check()
            if gpu != 'READY':
                if gpu == 'NO_GPU' and power:
                    await power.ensure_started()
                    heartbeat(lease, 'STARTING_GPU')
                else:
                    heartbeat(lease, {'NO_GPU': 'WAITING_FOR_GPU', 'MODELS_MISSING': 'GPU_MODELS_MISSING',
                                      'GPU_BUSY': 'GPU_BUSY'}[gpu])
                await asyncio.sleep(10)
                continue
            row = claim(lease)
            if row is None:
                continue
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'scripts.video.worker', '--job', str(row['id']), start_new_session=True)
            try:
                async with asyncio.timeout(7200):
                    while process.returncode is None:
                        heartbeat(lease, 'BUSY')
                        if power:
                            power.mark_busy()
                        await asyncio.sleep(2)
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


async def child(job_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM video_jobs WHERE id=%s AND status='RUNNING'", (job_id,)).fetchone()
    if not row:
        raise SystemExit('Job is not running')
    try:
        await run_job(row)
    except Exception as exc:
        reason = getattr(exc, 'reason', None) or str(exc) or type(exc).__name__
        update(row['id'], 'FAILED', status='FAILED', error='VIDEO_GENERATION_FAILED', message=reason)
        logging.error('Video job %s failed (%s); inspect local pipeline report', row['id'], type(exc).__name__)
        raise SystemExit(1)


if __name__ == '__main__':
    import sys
    from uuid import UUID
    if len(sys.argv) == 3 and sys.argv[1] == '--job':
        asyncio.run(child(UUID(sys.argv[2])))
    else:
        asyncio.run(main())
