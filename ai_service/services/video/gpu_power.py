"""Optional on-demand GPU: create a Pod when jobs wait, delete it after the queue stays idle.

A stopped RunPod Pod stays tied to its original machine and often cannot restart once that machine's GPU is
rented ("no instances currently available"). Models live on a network volume instead, so the worker creates a
fresh Pod on that volume with whichever allowed GPU type is free, and deletes it when idle. Only Pods whose name
starts with the configured prefix are ever started, created or deleted.
"""
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import time

import httpx

API = 'https://rest.runpod.io/v1/pods'
DEFAULT_GPU_TYPES = ('NVIDIA A100 80GB PCIe', 'NVIDIA A100-SXM4-80GB')
DEFAULT_IMAGE = 'runpod/comfyui:1.4.1-comfyuiv0.35.0-cuda12.8'
GATE_PORT = 8189
# The Pod publishes only the password gate (deploy/runpod-gpu/gate.py on the volume), never ComfyUI's 8188.
# gpu_boot.sh starts the gate, downloads the models to the Pod's own disk, then runs the image's /start.sh.
# Its text is sent as the start command, so the Pod needs nothing on the volume beyond gate.py and the downloader.
_GPU_DIR = Path(__file__).resolve().parents[3] / 'deploy' / 'runpod-gpu'


def start_command():
    """gpu_boot.sh preceded by the current gate.py, so a Pod or serverless worker never runs a stale gate."""
    gate = (_GPU_DIR / 'gate.py').read_text()
    assert 'AUDIGO_GATE_EOF' not in gate
    return (f"mkdir -p /local && cat > /local/gate.py <<'AUDIGO_GATE_EOF'\n{gate}AUDIGO_GATE_EOF\n"
            + (_GPU_DIR / 'gpu_boot.sh').read_text())


START_COMMAND = start_command()


@dataclass(frozen=True)
class PowerSettings:
    api_key: str
    volume_id: str
    gate_token: str = ''
    prefix: str = 'audigo-video-gpu-'
    gpu_types: tuple = DEFAULT_GPU_TYPES
    image: str = DEFAULT_IMAGE
    public_key: str = ''
    idle_stop_seconds: int = 600
    start_retry_seconds: int = 120

    @classmethod
    def from_env(cls):
        """Returns None unless VIDEO_GPU_AUTOSTART=1 with a RunPod API key and the models' network volume."""
        if os.environ.get('VIDEO_GPU_AUTOSTART') != '1':
            return None
        api_key, volume = os.environ.get('RUNPOD_API_KEY', ''), os.environ.get('VIDEO_GPU_VOLUME_ID', '')
        prefix = os.environ.get('RUNPOD_POD_PREFIX', 'audigo-video-gpu-')
        token = os.environ.get('COMFYUI_API_TOKEN', '')
        if not api_key or not re.fullmatch(r'[a-z0-9]+', volume) or len(prefix) < 6 or len(token) < 32:
            raise SystemExit('VIDEO_GPU_AUTOSTART=1 requires RUNPOD_API_KEY, VIDEO_GPU_VOLUME_ID, RUNPOD_POD_PREFIX '
                             'and a 32+ character COMFYUI_API_TOKEN for the GPU password gate')
        types = tuple(t.strip() for t in os.environ.get('VIDEO_GPU_TYPES', '').split(',') if t.strip())
        return cls(api_key, volume, token, prefix, types or DEFAULT_GPU_TYPES, os.environ.get('VIDEO_GPU_IMAGE', DEFAULT_IMAGE),
                   os.environ.get('VIDEO_GPU_PUBLIC_KEY', ''), int(os.environ.get('VIDEO_GPU_IDLE_STOP_SECONDS', 600)),
                   int(os.environ.get('VIDEO_GPU_START_RETRY_SECONDS', 120)))


class GpuPower:
    """Tracks the last start attempt and last queue activity; one instance per worker."""

    def __init__(self, settings: PowerSettings, *, transport=None, clock=time.monotonic):
        self.settings, self.transport, self.clock = settings, transport, clock
        self.last_start = None
        self.last_busy = clock()

    async def _request(self, method, path='', **kwargs):
        async with httpx.AsyncClient(transport=self.transport, timeout=60) as http:
            response = await http.request(method, API + path, headers={'Authorization': f'Bearer {self.settings.api_key}'},
                                          **kwargs)
            if response.status_code >= 400:
                raise httpx.HTTPStatusError(response.text[:300], request=response.request, response=response)
            return response.json() if response.content else None

    async def our_pods(self):
        pods = await self._request('GET')
        return [pod for pod in pods if str(pod.get('name', '')).startswith(self.settings.prefix)]

    def _create_body(self):
        body = {'name': f'{self.settings.prefix}{int(time.time())}', 'imageName': self.settings.image,
                'cloudType': 'SECURE', 'computeType': 'GPU', 'gpuTypeIds': list(self.settings.gpu_types),
                'gpuCount': 1, 'networkVolumeId': self.settings.volume_id, 'volumeMountPath': '/workspace',
                'containerDiskInGb': 100, 'ports': [f'{GATE_PORT}/http', '22/tcp'],  # 100 GB: 53 GB of local model copies plus inputs and outputs
                'dockerEntrypoint': ['bash', '-c'], 'dockerStartCmd': [START_COMMAND],
                'env': {'COMFYUI_API_TOKEN': self.settings.gate_token}}
        if self.settings.public_key:
            body['env']['PUBLIC_KEY'] = self.settings.public_key
        return body

    async def ensure_started(self):
        """Have one of our Pods running: keep a running one, else restart a stopped one, else create a new one.
        Attempts are spaced by start_retry_seconds; when no GPU is free the jobs simply stay queued."""
        self.last_busy = self.clock()
        if self.last_start is not None and self.clock() - self.last_start < self.settings.start_retry_seconds:
            return False
        self.last_start = self.clock()
        try:
            pods = await self.our_pods()
            if any(pod.get('desiredStatus') == 'RUNNING' for pod in pods):
                return False  # Still booting; the readiness check decides when it can take work.
            for pod in pods:
                try:
                    await self._request('POST', f"/{pod['id']}/start")
                    logging.info('Restarted GPU Pod %s', pod['id'])
                    return True
                except httpx.HTTPStatusError:
                    # Its machine has no free GPU; a stopped Pod holds nothing the volume does not.
                    await self._request('DELETE', f"/{pod['id']}")
                    logging.info('Deleted stopped GPU Pod %s that could not restart', pod['id'])
            created = await self._request('POST', json=self._create_body())
            logging.info('Created GPU Pod %s on volume %s', created.get('id'), self.settings.volume_id)
            return True
        except httpx.HTTPError as exc:
            logging.warning('No GPU Pod available yet (%s); jobs stay queued', str(exc)[:160])
            return False

    def mark_busy(self):
        self.last_busy = self.clock()

    async def stop_if_idle(self):
        """Delete our Pods once the queue has been empty for idle_stop_seconds; models stay on the volume."""
        if self.clock() - self.last_busy < self.settings.idle_stop_seconds:
            return False
        try:
            pods = await self.our_pods()
            for pod in pods:
                await self._request('DELETE', f"/{pod['id']}")
                logging.info('Deleted idle GPU Pod %s', pod['id'])
        except httpx.HTTPError as exc:
            logging.warning('GPU Pod cleanup failed (%s)', str(exc)[:160])
            return False
        self.last_busy = self.clock()  # Check again only after another idle period.
        self.last_start = None
        return bool(pods)
