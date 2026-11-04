"""Durable results on RunPod Network Volume via its S3 gateway (not AWS S3)."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import boto3
from botocore.config import Config
from dotenv import dotenv_values

from ai_service.core.config import ENV_FILE
from ai_service.services.video.errors import VideoError


@dataclass(frozen=True)
class VolumeSettings:
    volume_id: str
    region: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self):
        if not re.fullmatch(r'[a-z0-9]{5,64}', self.volume_id):
            raise ValueError('invalid volume id')
        if not re.fullmatch(r'[A-Z]{2,3}-[A-Z0-9]+-\d+', self.region):
            raise ValueError('invalid RunPod data center')
        if not self.access_key or not self.secret_key:
            raise VideoError('runpod_storage_credentials_missing')

    @classmethod
    def from_env(cls):
        values = {**dotenv_values(ENV_FILE), **os.environ}
        return cls(values.get('RUNPOD_STORAGE_VOLUME_ID', ''),
                   values.get('RUNPOD_STORAGE_REGION', ''),
                   values.get('RUNPOD_S3_ACCESS_KEY_ID', ''),
                   values.get('RUNPOD_S3_SECRET_ACCESS_KEY', ''))


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class VolumeResults:
    def __init__(self, settings: VolumeSettings, *, client=None):
        self.settings = settings
        self.client = client or boto3.client(
            's3', endpoint_url=f'https://s3api-{settings.region.lower()}.runpod.io',
            region_name=settings.region, aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            config=Config(signature_version='s3v4', connect_timeout=15, read_timeout=60,
                          retries={'max_attempts': 3}, s3={'addressing_style': 'path'}))

    @staticmethod
    def prefix(job_id):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', job_id):
            raise VideoError('storage_job_id_invalid')
        return f'jobs/{job_id}/'

    def _verify(self, key, expected_hash, expected_size):
        response = self.client.get_object(Bucket=self.settings.volume_id, Key=key)
        body = response['Body']
        digest, size = hashlib.sha256(), 0
        try:
            for chunk in iter(lambda: body.read(1024 * 1024), b''):
                digest.update(chunk); size += len(chunk)
                if size > expected_size:
                    raise VideoError('storage_integrity_failed')
        finally:
            body.close()
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise VideoError('storage_integrity_failed')

    def publish(self, video: Path, thumbnail: Path, media: dict):
        """Fresh ID per publication; commit marker is written last, after full read-back."""
        job_id = uuid4().hex
        prefix = self.prefix(job_id)
        assets = {}
        try:
            for name, path, content_type in [('video', Path(video), 'video/mp4'),
                                              ('thumbnail', Path(thumbnail), 'image/jpeg')]:
                if not path.is_file() or path.stat().st_size == 0:
                    raise VideoError('storage_asset_missing')
                key = prefix + ('final.mp4' if name == 'video' else 'thumbnail.jpg')
                size, digest = path.stat().st_size, file_hash(path)
                self.client.upload_file(str(path), self.settings.volume_id, key,
                                        ExtraArgs={'ContentType': content_type})
                self._verify(key, digest, size)
                assets[name] = {'key': key, 'sha256': digest, 'size_bytes': size}
            result = {'schema_version': 1, 'job_id': job_id, 'status': 'COMPLETED',
                      'storage': 'runpod_network_volume', 'volume_id': self.settings.volume_id,
                      'region': self.settings.region, 'assets': assets,
                      'media': {k: media[k] for k in ('duration_seconds', 'width', 'height', 'fps') if k in media}}
            raw = json.dumps(result, ensure_ascii=False).encode()
            self.client.put_object(Bucket=self.settings.volume_id, Key=prefix+'result.json',
                                   Body=raw, ContentType='application/json')
            if self.inspect(job_id) != result:
                raise VideoError('storage_commit_verification_failed')
            return result
        except VideoError:
            raise
        except Exception as exc:
            raise VideoError('runpod_storage_publish_failed') from exc

    def inspect(self, job_id):
        prefix = self.prefix(job_id)
        try:
            response = self.client.get_object(Bucket=self.settings.volume_id, Key=prefix+'result.json')
            body = response['Body']
            try:
                raw = body.read(65537)
            finally:
                body.close()
            if len(raw) > 65536:
                raise ValueError('oversized result')
            result = json.loads(raw)
            if (result['job_id'] != job_id or result['status'] != 'COMPLETED'
                    or result['volume_id'] != self.settings.volume_id
                    or result['region'] != self.settings.region):
                raise ValueError('result mismatch')
            for name, filename in [('video', 'final.mp4'), ('thumbnail', 'thumbnail.jpg')]:
                asset = result['assets'][name]
                if (asset['key'] != prefix+filename or not re.fullmatch('[0-9a-f]{64}', asset['sha256'])
                        or type(asset['size_bytes']) is not int or asset['size_bytes'] <= 0):
                    raise ValueError('invalid asset')
            return result
        except Exception as exc:
            raise VideoError('runpod_storage_result_unavailable') from exc

    def download(self, job_id, destination: Path, *, kind='video'):
        if kind not in {'video', 'thumbnail'}:
            raise VideoError('storage_asset_kind_invalid')
        asset = self.inspect(job_id)['assets'][kind]
        destination = Path(destination)
        if destination.exists():
            raise VideoError('storage_destination_exists')
        temporary = destination.with_name(destination.name + '.' + uuid4().hex + '.part')
        try:
            self.client.download_file(self.settings.volume_id, asset['key'], str(temporary))
            if temporary.stat().st_size != asset['size_bytes'] or file_hash(temporary) != asset['sha256']:
                raise VideoError('storage_integrity_failed')
            # Atomic, exclusive publication on the same filesystem.
            os.link(temporary, destination)
            return destination
        except VideoError:
            raise
        except Exception as exc:
            raise VideoError('runpod_storage_download_failed') from exc
        finally:
            temporary.unlink(missing_ok=True)
