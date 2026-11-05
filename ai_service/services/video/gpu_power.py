"""영상 요청에 맞는 RunPod GPU를 확보하고, 대기열이 비면 유휴 Pod를 정리한다.

모델은 네트워크 볼륨에 보관한다. 정지된 Pod를 다시 켤 수 없으면 같은 볼륨으로
새 Pod를 만든다. 설정한 이름 접두사를 가진 Pod만 관리한다.
"""
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import time

import httpx

from ai_service.clients.runpod import matches_single_gpu

API = 'https://rest.runpod.io/v1/pods'
RTX_PRO_6000 = 'NVIDIA RTX PRO 6000 Blackwell Server Edition'
A100_PCIE = 'NVIDIA A100 80GB PCIe'
DEFAULT_GPU_TYPES = (RTX_PRO_6000, A100_PCIE)
AUTO_GPU = 'AUTO'
GPU_LABELS = {RTX_PRO_6000: 'RTX PRO 6000 96GB ×1', A100_PCIE: 'A100 PCIe 80GB ×1',
              AUTO_GPU: '재고 있는 GPU 자동 선택'}
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


def configured_gpu_types() -> tuple[str, ...]:
    """운영자가 허용한 모델을 설정 순서대로 반환한다."""
    values = (value.strip() for value in os.environ.get('VIDEO_GPU_TYPES', '').split(','))
    return tuple(dict.fromkeys(value for value in values if value and value != AUTO_GPU)) or DEFAULT_GPU_TYPES


def selectable_gpu_types(settings) -> tuple[str, ...]:
    """자동 할당 환경에서만 작업별 GPU 선택을 허용한다."""
    if os.environ.get('VIDEO_GPU_AUTOSTART') == '1' and settings.endpoint_mode == 'runpod' and not settings.runpod_pod_id:
        return configured_gpu_types()
    return ()


def default_gpu_type(gpu_types: tuple[str, ...]) -> str | None:
    if RTX_PRO_6000 in gpu_types:
        return RTX_PRO_6000
    return gpu_types[0] if gpu_types else None


def gpu_selection_allowed(selection: str, gpu_types: tuple[str, ...]) -> bool:
    return selection in gpu_types or (selection == AUTO_GPU and bool(gpu_types))


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
        """자동 시작 설정이 켜져 있을 때 GPU 할당 설정을 불러온다."""
        if os.environ.get('VIDEO_GPU_AUTOSTART') != '1':
            return None
        api_key, volume = os.environ.get('RUNPOD_API_KEY', ''), os.environ.get('VIDEO_GPU_VOLUME_ID', '')
        prefix = os.environ.get('RUNPOD_POD_PREFIX', 'audigo-video-gpu-')
        token = os.environ.get('COMFYUI_API_TOKEN', '')
        if not api_key or not re.fullmatch(r'[a-z0-9]+', volume) or len(prefix) < 6 or len(token) < 32:
            raise SystemExit('VIDEO_GPU_AUTOSTART=1 requires RUNPOD_API_KEY, VIDEO_GPU_VOLUME_ID, RUNPOD_POD_PREFIX '
                             'and a 32+ character COMFYUI_API_TOKEN for the GPU password gate')
        return cls(api_key, volume, token, prefix, configured_gpu_types(), os.environ.get('VIDEO_GPU_IMAGE', DEFAULT_IMAGE),
                   os.environ.get('VIDEO_GPU_PUBLIC_KEY', ''), int(os.environ.get('VIDEO_GPU_IDLE_STOP_SECONDS', 600)),
                   int(os.environ.get('VIDEO_GPU_START_RETRY_SECONDS', 120)))


class GpuPower:
    """작업 실행기마다 GPU 할당 시도와 마지막 작업 시각을 관리한다."""

    def __init__(self, settings: PowerSettings, *, transport=None, clock=time.monotonic):
        self.settings, self.transport, self.clock = settings, transport, clock
        self.last_start = {}
        self.capacity_waiting = set()
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

    def _create_body(self, gpu_type=None):
        if gpu_type is not None and not gpu_selection_allowed(gpu_type, self.settings.gpu_types):
            raise ValueError('GPU 모델이 자동 할당 허용 목록에 없습니다.')
        candidates = [gpu_type] if gpu_type and gpu_type != AUTO_GPU else list(self.settings.gpu_types)
        body = {'name': f'{self.settings.prefix}{int(time.time())}', 'imageName': self.settings.image,
                'cloudType': 'SECURE', 'computeType': 'GPU',
                'gpuTypeIds': candidates, 'gpuTypePriority': 'availability',
                'gpuCount': 1, 'networkVolumeId': self.settings.volume_id, 'volumeMountPath': '/workspace',
                'containerDiskInGb': 100, 'ports': [f'{GATE_PORT}/http', '22/tcp'],  # 100 GB: 53 GB of local model copies plus inputs and outputs
                'dockerEntrypoint': ['bash', '-c'], 'dockerStartCmd': [START_COMMAND],
                'env': {'COMFYUI_API_TOKEN': self.settings.gate_token}}
        if self.settings.public_key:
            body['env']['PUBLIC_KEY'] = self.settings.public_key
        return body

    async def ensure_started(self, gpu_type=None):
        """요청과 일치하는 Pod를 재사용하거나 새로 만든다. 재고가 없으면 대기열에 둔다."""
        self.last_busy = self.clock()
        last_attempt = self.last_start.get(gpu_type)
        if last_attempt is not None and self.clock() - last_attempt < self.settings.start_retry_seconds:
            return False
        self.last_start[gpu_type] = self.clock()
        self.capacity_waiting.discard(gpu_type)
        body = self._create_body(gpu_type)
        try:
            pods = await self.our_pods()
            if gpu_type is not None:
                pods = [pod for pod in pods if any(matches_single_gpu(pod, model) for model in body['gpuTypeIds'])]
            if any(pod.get('desiredStatus') == 'RUNNING' for pod in pods):
                return False  # 부팅 중이면 준비 상태 확인에서 작업 가능 여부를 판단한다.
            for pod in pods:
                try:
                    await self._request('POST', f"/{pod['id']}/start")
                    logging.info('Restarted GPU Pod %s', pod['id'])
                    return True
                except httpx.HTTPStatusError:
                    # 정지한 Pod를 다시 시작할 수 없으면 모델 볼륨은 남기고 Pod만 교체한다.
                    await self._request('DELETE', f"/{pod['id']}")
                    logging.info('Deleted stopped GPU Pod %s that could not restart', pod['id'])
            created = await self._request('POST', json=body)
            logging.info('Created GPU Pod %s on volume %s', created.get('id'), self.settings.volume_id)
            return True
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError) and any(
                phrase in exc.response.text.lower() for phrase in (
                    'no instances currently available', 'no available gpu', 'insufficient gpu capacity',
                )
            ):
                self.capacity_waiting.add(gpu_type)
            logging.warning('No GPU Pod available yet (%s); jobs stay queued', str(exc)[:160])
            return False

    def mark_busy(self):
        self.last_busy = self.clock()

    async def stop_if_idle(self):
        """대기열이 일정 시간 비면 관리 중인 Pod를 삭제한다. 모델은 볼륨에 남긴다."""
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
        self.last_busy = self.clock()  # 같은 유휴 시간이 지난 뒤 다시 확인한다.
        self.last_start.clear()
        self.capacity_waiting.clear()
        return bool(pods)
