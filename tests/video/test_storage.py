import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ai_service.services.video.storage import VolumeResults, VolumeSettings
from ai_service.services.video.errors import VideoError


class MemoryGateway:
    def __init__(self):
        self.files = {}
        self.corrupt = False

    def upload_file(self, filename, bucket, key, **kwargs):
        self.files[key] = Path(filename).read_bytes()

    def put_object(self, **kwargs):
        self.files[kwargs['Key']] = kwargs['Body']

    def get_object(self, **kwargs):
        data = self.files[kwargs['Key']]
        return {'Body': io.BytesIO(b'corrupt' if self.corrupt else data)}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(b'corrupt' if self.corrupt else self.files[key])


@pytest.fixture
def setup_store(tmp_path):
    gateway = MemoryGateway()
    store = VolumeResults(VolumeSettings('ioqbto3qji', 'US-WA-1', 'test', 'secret'), client=gateway)
    video, image = tmp_path/'source.mp4', tmp_path/'source.jpg'
    video.write_bytes(b'fixture-video'); image.write_bytes(b'fixture-thumbnail')
    return store, gateway, video, image


def test_publish_commit_and_download_round_trip_without_local_paths(setup_store, tmp_path):
    store, gateway, video, image = setup_store
    result = store.publish(video, image, {'duration_seconds': 30, 'private': str(video)})
    assert str(video) not in json.dumps(result)
    assert result['media'] == {'duration_seconds': 30}
    assert len(gateway.files) == 3
    assert store.inspect(result['job_id']) == result
    target = tmp_path/'download.mp4'
    store.download(result['job_id'], target)
    assert target.read_bytes() == video.read_bytes()
    with pytest.raises(VideoError, match='destination_exists'):
        store.download(result['job_id'], target)


def test_failed_asset_verification_never_commits_completion(setup_store):
    store, gateway, video, image = setup_store
    gateway.corrupt = True
    with pytest.raises(VideoError, match='integrity_failed'):
        store.publish(video, image, {})
    assert not any(key.endswith('result.json') for key in gateway.files)


def test_corrupt_download_does_not_create_final_file(setup_store, tmp_path):
    store, gateway, video, image = setup_store
    result = store.publish(video, image, {})
    original = gateway.download_file
    gateway.download_file = lambda bucket, key, path: Path(path).write_bytes(b'bad')
    target = tmp_path/'bad.mp4'
    with pytest.raises(VideoError, match='integrity_failed'):
        store.download(result['job_id'], target)
    assert not target.exists()
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('job_id', ['../secret', '/root', 'a/b', '', 'a'*101])
def test_job_path_traversal_rejected(setup_store, job_id):
    store, *_ = setup_store
    with pytest.raises(VideoError, match='job_id_invalid'):
        store.inspect(job_id)


def test_manifest_cannot_point_at_another_job(setup_store):
    store, gateway, video, image = setup_store
    result = store.publish(video, image, {})
    result['assets']['video']['key'] = 'jobs/other/final.mp4'
    gateway.files[f'jobs/{result["job_id"]}/result.json'] = json.dumps(result).encode()
    with pytest.raises(VideoError, match='result_unavailable'):
        store.inspect(result['job_id'])


def test_missing_credentials_fail_before_requests():
    with pytest.raises(VideoError, match='credentials_missing'):
        VolumeSettings('ioqbto3qji', 'US-WA-1', '', '')


def test_publish_missing_credentials_prevents_generation():
    import asyncio
    from argparse import Namespace
    from unittest.mock import patch, AsyncMock
    from scripts.video.run import execute
    args = Namespace(publish_to_runpod=True, mock=False, preview_only=False)
    with patch('ai_service.services.video.storage.VolumeSettings.from_env',
               side_effect=VideoError('runpod_storage_credentials_missing')), \
         patch('ai_service.services.video.pipeline.VideoPipeline.generate', new_callable=AsyncMock) as generate:
        with pytest.raises(VideoError, match='credentials_missing'):
            asyncio.run(execute(args))
        generate.assert_not_awaited()
