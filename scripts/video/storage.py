"""Publish or retrieve videos from RunPod storage without GPU inference."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from ai_service.services.video.storage import VolumeResults, VolumeSettings
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.media import Media
from ai_service.services.video.errors import VideoError


async def publish_video(video):
    store = VolumeResults(VolumeSettings.from_env())
    media = Media(VideoSettings())
    info = await media.probe(video)
    with tempfile.TemporaryDirectory() as folder:
        thumbnail = Path(folder)/'thumbnail.jpg'
        await media.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(video),
                         '-frames:v', '1', '-q:v', '2', str(thumbnail)])
        return await asyncio.to_thread(store.publish, video, thumbnail, info)


async def run(args):
    if args.command == 'publish':
        return await publish_video(args.video)
    store = VolumeResults(VolumeSettings.from_env())
    if args.command == 'inspect':
        return await asyncio.to_thread(store.inspect, args.job_id)
    path = await asyncio.to_thread(store.download, args.job_id, args.output, kind=args.kind)
    return {'downloaded_file': str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    publish = commands.add_parser('publish')
    publish.add_argument('--video', type=Path, required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--job-id', required=True)
    download = commands.add_parser('download')
    download.add_argument('--job-id', required=True)
    download.add_argument('--output', type=Path, required=True)
    download.add_argument('--kind', choices=['video', 'thumbnail'], default='video')
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args())), ensure_ascii=False, indent=2))
    except (VideoError, ValueError) as exc:
        print(json.dumps({'status': 'failed', 'reason': getattr(exc, 'reason', 'storage_configuration_invalid')}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
