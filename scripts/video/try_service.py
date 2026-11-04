"""Try the deployed video service end to end: upload photos, request a video, follow progress, print a playback URL.

Reads AUDIGO_API_TOKEN from the environment or .env. GPU generation is billed while the GPU Pod runs.

    uv run python -m scripts.video.try_service --photos ~/Downloads/album --limit 12
    uv run python -m scripts.video.try_service --job JOB_ID     # check an existing job again
"""
import argparse
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from dotenv import dotenv_values
import httpx

DEFAULT_URL = 'https://bggrh1188jif0b-8000.proxy.runpod.net'
PHOTO_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}


def follow(http, job_id, interval):
    last = None
    while True:
        job = http.get(f'/api/ai/v1/video-jobs/{job_id}').raise_for_status().json()
        line = f"{job['stage_label']} ({job['status']}, 실행기: {job['worker_state']})"
        if line != last:
            print(time.strftime('%H:%M:%S'), line, flush=True)
            last = line
        if job['status'] == 'FAILED':
            sys.exit(f"실패: {job['error_code']} {job.get('error_message') or ''}")
        if job['status'] == 'COMPLETED':
            play = http.get(f'/api/ai/v1/video-jobs/{job_id}/video', params={'response_format': 'json'}).raise_for_status().json()
            print(f"\n완성! 아래 주소를 브라우저에 붙여 넣으면 재생됩니다 ({play['expires_in'] // 60}분 동안 유효):\n{play['url']}")
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
    token = os.environ.get('AUDIGO_API_TOKEN') or dotenv_values('.env').get('AUDIGO_API_TOKEN')
    if not token:
        sys.exit('AUDIGO_API_TOKEN is not set (environment or .env)')
    headers = {'Authorization': f'Bearer {token}', 'X-Owner-ID': str(args.owner)}
    with httpx.Client(base_url=args.url, headers=headers, timeout=120) as http:
        if args.job:
            return follow(http, args.job, args.interval)
        photos = sorted(p for p in args.photos.expanduser().iterdir() if p.suffix.lower() in PHOTO_TYPES)[:args.limit]
        if len(photos) < 2:
            sys.exit('At least two photos are required')
        upload_ids = []
        for index, photo in enumerate(photos, 1):
            response = http.post('/api/ai/v1/uploads', content=photo.read_bytes(),
                                 headers={'Content-Type': PHOTO_TYPES[photo.suffix.lower()]})
            if response.status_code != 201:
                sys.exit(f'{photo.name} 업로드 실패: HTTP {response.status_code} {response.text[:200]}')
            upload_ids.append(response.json()['upload_id'])
            print(f'사진 업로드 {index}/{len(photos)}: {photo.name}', flush=True)
        body = {'request_id': str(uuid4()), 'travel_plan_id': args.travel, 'upload_ids': upload_ids,
                'duration_seconds': args.duration, 'memory_context': args.context}
        job = http.post('/api/ai/v1/video-jobs', json=body).raise_for_status().json()
        print(f"\n작업 접수됨. 작업 ID: {job['job_id']}\n(나중에 다시 확인: --job {job['job_id']})\n", flush=True)
        follow(http, job['job_id'], args.interval)


if __name__ == '__main__':
    main()
