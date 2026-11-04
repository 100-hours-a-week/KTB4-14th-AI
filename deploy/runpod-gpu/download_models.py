"""Download the pinned video and music models onto a RunPod network volume, once.

Run on any Pod that mounts the GPU region's network volume at /workspace (a cheap CPU Pod is enough):

    python3 download_models.py                       # default: /workspace/runpod-slim/ComfyUI/models
    python3 download_models.py --models-dir /path    # another ComfyUI models folder
    python3 download_models.py --check               # verify only, download nothing

The runpod/comfyui image keeps /workspace/runpod-slim/ComfyUI/models when it updates ComfyUI, so a GPU Pod
started on the same volume finds these files without downloading again. Files already present with the right
SHA-256 are skipped; interrupted downloads resume. Standard library only, so it runs on a bare Pod.
"""
import argparse
import hashlib
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

LTX_REVISION = '91eda216f820e5ecb3d17dc9fe3b7cdaf56b587d'
LTX_TEXT_REVISION = 'ccde4ba417d7900669fd56dd292a883cee11ff37'
ACE_REVISION = '371bb1680f8c88d8c1083f6ffb6330d3d55bff76'
HF = 'https://huggingface.co'

# (ComfyUI folder, file name, URL, size in bytes, SHA-256). tests/video/test_gpu_models.py keeps this in sync
# with the checkpoint names in VideoSettings, LTX_PROVENANCE and ace_music_downloads('turbo').
MODELS = [
    ('checkpoints', 'ltx-2.3-22b-distilled-fp8.safetensors',
     f'{HF}/Lightricks/LTX-2.3-fp8/resolve/{LTX_REVISION}/ltx-2.3-22b-distilled-fp8.safetensors',
     29531884062, 'd9646b6f2d5c42d337b23671634c43bfeece6989644f51b4a3aa088465ccd3b2'),
    ('text_encoders', 'gemma_3_12B_it_fp8_scaled.safetensors',
     f'{HF}/Comfy-Org/ltx-2/resolve/{LTX_TEXT_REVISION}/split_files/text_encoders/gemma_3_12B_it_fp8_scaled.safetensors',
     13205434827, '60216ce97c01c3a8753c2dfd0a89fc76e16fbe446d1a32ef8f1b528ac8bae466'),
    ('diffusion_models', 'acestep_v1.5_turbo.safetensors',
     f'{HF}/Comfy-Org/ACE-Step-1.5/resolve/{ACE_REVISION}/split_files/diffusion_models/acestep_v1.5_turbo.safetensors',
     4787825604, '3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7'),
    ('text_encoders', 'qwen_0.6b_ace15.safetensors',
     f'{HF}/Comfy-Org/ACE-Step-1.5/resolve/{ACE_REVISION}/split_files/text_encoders/qwen_0.6b_ace15.safetensors',
     1191588248, 'fd4590c82153b8ddb67e15a2e7aaa8afa8b83a858c8a9b82a4831063156aa7a7'),
    ('text_encoders', 'qwen_1.7b_ace15.safetensors',
     f'{HF}/Comfy-Org/ACE-Step-1.5/resolve/{ACE_REVISION}/split_files/text_encoders/qwen_1.7b_ace15.safetensors',
     3708523360, 'ed63e9247d1f55f3ace04fa11e95b085fc82d459c82c5626f0b2e37b91ebd710'),
    ('vae', 'ace_1.5_vae.safetensors',
     f'{HF}/Comfy-Org/ACE-Step-1.5/resolve/{ACE_REVISION}/split_files/vae/ace_1.5_vae.safetensors',
     337431732, '6de92e3a862acd287e08b024ac90f0783a8635451b728721a33ff03565bcb2bb'),
]
CHUNK = 8 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def fetch(url, partial, size):
    """Append to `partial` from where it stopped; Hugging Face honours byte ranges."""
    for attempt in range(5):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= size:
            return
        request = Request(url, headers={'Range': f'bytes={offset}-', 'User-Agent': 'audigo-model-setup'})
        try:
            with urlopen(request, timeout=60) as response, partial.open('ab') as output:
                if offset and response.status != 206:
                    raise OSError('server ignored the resume range')
                started, done = time.monotonic(), offset
                for block in iter(lambda: response.read(CHUNK), b''):
                    output.write(block)
                    done += len(block)
                    rate = (done - offset) / max(time.monotonic() - started, 1e-6) / 1e6
                    print(f'\r  {done / 1e9:6.2f}/{size / 1e9:.2f} GB  {rate:6.1f} MB/s', end='', flush=True)
            print()
            return
        except OSError as exc:
            print(f'\n  interrupted ({type(exc).__name__}); retrying in {5 * (attempt + 1)} s', flush=True)
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f'Download kept failing: {url}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--models-dir', type=Path, default=Path('/workspace/runpod-slim/ComfyUI/models'))
    parser.add_argument('--check', action='store_true', help='verify existing files only')
    args = parser.parse_args()
    needed = sum(size for *_, size, _ in MODELS)
    missing = []
    for folder, name, url, size, digest in MODELS:
        target = args.models_dir / folder / name
        print(f'{folder}/{name} ({size / 1e9:.1f} GB)', flush=True)
        if target.is_file() and target.stat().st_size == size and sha256(target) == digest:
            print('  ok', flush=True)
            continue
        if args.check:
            missing.append(name)
            print('  missing or damaged', flush=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        free = os.statvfs(target.parent)
        if free.f_bavail * free.f_frsize < size * 1.05:
            raise SystemExit(f'Not enough free space for {name}; the full set needs {needed / 1e9:.0f} GB')
        partial = target.with_name(target.name + '.partial')
        fetch(url, partial, size)
        if partial.stat().st_size != size or sha256(partial) != digest:
            partial.unlink()
            raise SystemExit(f'{name} failed its checksum; run again to retry')
        partial.replace(target)
        print('  downloaded and verified', flush=True)
    if missing:
        raise SystemExit(f'{len(missing)} model file(s) missing: ' + ', '.join(missing))
    print(f'All {len(MODELS)} model files are ready ({needed / 1e9:.0f} GB) in {args.models_dir}')


if __name__ == '__main__':
    sys.exit(main())
