import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest

from ai_service.services.video.ace_music import ACE_MODEL_REVISION, ace_music_downloads
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.ltx import LTX_PROVENANCE

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'runpod-gpu' / 'download_models.py'
spec = importlib.util.spec_from_file_location('download_models', SCRIPT)
download_models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(download_models)


def test_manifest_matches_models_the_pipeline_loads():
    names = {(folder, name) for folder, name, *_ in download_models.MODELS}
    settings = VideoSettings()
    assert ('checkpoints', settings.ltx_checkpoint) in names
    assert ('text_encoders', settings.ltx_text_encoder) in names
    assert download_models.LTX_REVISION == LTX_PROVENANCE['model_revision']
    assert download_models.LTX_TEXT_REVISION == LTX_PROVENANCE['text_encoder_revision']
    assert download_models.ACE_REVISION == ACE_MODEL_REVISION
    by_name = {name: (folder, url, size, digest) for folder, name, url, size, digest in download_models.MODELS}
    for item in ace_music_downloads('turbo'):
        assert by_name[item['filename']] == (item['directory'], item['url'], item['size_bytes'], item['sha256'])


def test_verified_files_are_skipped_and_check_mode_reports_missing(tmp_path, monkeypatch):
    data = b'model bytes'
    good = ('checkpoints', 'a.safetensors', 'https://example.invalid/a', len(data), hashlib.sha256(data).hexdigest())
    absent = ('vae', 'b.safetensors', 'https://example.invalid/b', 5, '0' * 64)
    (tmp_path / 'checkpoints').mkdir()
    (tmp_path / 'checkpoints' / 'a.safetensors').write_bytes(data)
    monkeypatch.setattr(download_models, 'MODELS', [good])
    monkeypatch.setattr(sys, 'argv', ['x', '--models-dir', str(tmp_path)])
    monkeypatch.setattr(download_models, 'fetch', lambda *a: pytest.fail('verified file must not be downloaded'))
    download_models.main()
    monkeypatch.setattr(download_models, 'MODELS', [good, absent])
    monkeypatch.setattr(sys, 'argv', ['x', '--models-dir', str(tmp_path), '--check'])
    with pytest.raises(SystemExit, match='b.safetensors'):
        download_models.main()


def test_damaged_download_is_discarded(tmp_path, monkeypatch):
    entry = ('vae', 'c.safetensors', 'https://example.invalid/c', 4, hashlib.sha256(b'good').hexdigest())
    monkeypatch.setattr(download_models, 'MODELS', [entry])
    monkeypatch.setattr(sys, 'argv', ['x', '--models-dir', str(tmp_path)])
    monkeypatch.setattr(download_models, 'fetch', lambda url, partial, size: partial.write_bytes(b'evil'))
    with pytest.raises(SystemExit, match='checksum'):
        download_models.main()
    assert not list((tmp_path / 'vae').iterdir())
