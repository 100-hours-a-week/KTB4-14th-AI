"""Try the deployed video service end to end: upload photos, request a video, follow progress, print a playback URL.

Reads AUDIGO_API_TOKEN from the environment or .env. GPU generation is billed while the GPU Pod runs.

    uv run python -m scripts.video.try_service --photos ~/Downloads/album --limit 12
    uv run python -m scripts.video.try_service --job JOB_ID     # check an existing job again
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from dotenv import dotenv_values, find_dotenv
import httpx

DEFAULT_URL = 'https://bggrh1188jif0b-8000.proxy.runpod.net'
PHOTO_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}
# Same as the test page: the film is 1920x1080, so a 1920 px long edge loses nothing it uses, and most of the
# upload time is the trip to the US server, so smaller files and parallel requests are what make it faster.
MAX_EDGE, PARALLEL = 1920, 6


def shrunk(photo: Path):
    """(bytes, content type) of the photo as a JPEG with a 1920 px long edge, or the original when that is
    not smaller or Pillow cannot read it (the server then decides)."""
    data = photo.read_bytes()
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            out = io.BytesIO()
            image.save(out, 'JPEG', quality=88)
        if out.tell() < len(data):
            return out.getvalue(), 'image/jpeg'
    except Exception:
        pass
    return data, PHOTO_TYPES[photo.suffix.lower()]


def follow(http, job_id, interval):
    last = None
    while True:
        try:
            job = http.get(f'/api/ai/v1/video-jobs/{job_id}').raise_for_status().json()
        except httpx.TransportError as error:  # a brief network drop must not end the watch
            print(time.strftime('%H:%M:%S'), f'연결 오류, 다시 시도합니다 ({type(error).__name__})', flush=True)
            time.sleep(interval)
            continue
        line = f"{job['stage_label']} ({job['status']}, 실행기: {job['worker_state']})"
        if line != last:
            print(time.strftime('%H:%M:%S'), line, flush=True)
            last = line
        if job['status'] == 'FAILED':
            sys.exit(f"실패: {job['error_code']} {job.get('error_message') or ''}")
        if job['status'] == 'COMPLETED':
            play = http.get(f'/api/ai/v1/video-jobs/{job_id}/video', params={'response_format': 'json'}).raise_for_status().json()
            print(f"\n완성! 아래 주소를 브라우저에 붙여 넣으면 재생됩니다:\n{play['url']}")
            return
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--photos', type=Path, help='folder of JPEG/PNG/WebP photos')
    group.add_argument('--job', help='existing job ID to follow')
    parser.add_argument('--limit', type=int, default=12, help='upload at most this many photos (default 12)')
    parser.add_argument('--owner', type=int, default=1, help='test user ID (default 1)')
    parser.add_argument('--travel', type=int, default=101, help='test travel plan ID (default 101)')
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--context', default='', help='short memory description, e.g. "강릉 바다 여행"')
    parser.add_argument('--url', default=os.environ.get('VIDEO_SERVICE_URL', DEFAULT_URL))
    parser.add_argument('--interval', type=int, default=10, help='seconds between status checks')
    args = parser.parse_args()
    # Walk up from the current folder so a git worktree inside the repo still finds the repo's .env.
    token = os.environ.get('AUDIGO_API_TOKEN') or dotenv_values(find_dotenv(usecwd=True)).get('AUDIGO_API_TOKEN')
    if not token:
        sys.exit('AUDIGO_API_TOKEN을 찾지 못했습니다. 프로젝트 폴더의 .env에 있어야 합니다.')
    headers = {'Authorization': f'Bearer {token}', 'X-Owner-ID': str(args.owner)}
    with httpx.Client(base_url=args.url, headers=headers, timeout=120) as http:
        if args.job:
            return follow(http, args.job, args.interval)
        photos = sorted(p for p in args.photos.expanduser().iterdir() if p.suffix.lower() in PHOTO_TYPES)[:args.limit]
        if len(photos) < 2:
            sys.exit('At least two photos are required')
        def upload(photo):
            data, kind = shrunk(photo)
            response = http.post('/api/ai/v1/uploads', content=data, headers={'Content-Type': kind})
            if response.status_code != 201:
                sys.exit(f'{photo.name} 업로드 실패: HTTP {response.status_code} {response.text[:200]}')
            return response.json()['upload_id']

        started = time.monotonic()
        with ThreadPoolExecutor(PARALLEL) as pool:
            upload_ids = list(pool.map(upload, photos))  # keeps the album order
        print(f'사진 {len(photos)}장 업로드 완료 ({time.monotonic() - started:.1f}초)', flush=True)
        body = {'request_id': str(uuid4()), 'travel_plan_id': args.travel, 'upload_ids': upload_ids,
                'duration_seconds': args.duration, 'memory_context': args.context}
        job = http.post('/api/ai/v1/video-jobs', json=body).raise_for_status().json()
        print(f"\n작업 접수됨. 작업 ID: {job['job_id']}\n(나중에 다시 확인: --job {job['job_id']})\n", flush=True)
        follow(http, job['job_id'], args.interval)


if __name__ == '__main__':
    main()
