"""Optional GPU Pod power control: start it when jobs wait, stop it after the queue stays idle."""
from dataclasses import dataclass
import logging
import os
import re
import time

import httpx

API = 'https://rest.runpod.io/v1/pods'


@dataclass(frozen=True)
class PowerSettings:
    pod_id: str
    api_key: str
    idle_stop_seconds: int = 600
    start_retry_seconds: int = 300

    @classmethod
    def from_env(cls):
        """Returns None unless VIDEO_GPU_AUTOSTART=1 and a single GPU Pod ID and API key are configured."""
        if os.environ.get('VIDEO_GPU_AUTOSTART') != '1':
            return None
        pod_id, api_key = os.environ.get('RUNPOD_POD_ID', ''), os.environ.get('RUNPOD_API_KEY', '')
        if not re.fullmatch(r'[a-zA-Z0-9]+', pod_id) or not api_key:
            raise SystemExit('VIDEO_GPU_AUTOSTART=1 requires RUNPOD_POD_ID and RUNPOD_API_KEY')
        return cls(pod_id, api_key, int(os.environ.get('VIDEO_GPU_IDLE_STOP_SECONDS', 600)),
                   int(os.environ.get('VIDEO_GPU_START_RETRY_SECONDS', 300)))


class GpuPower:
    """Tracks the last start attempt and last queue activity; one instance per worker."""

    def __init__(self, settings: PowerSettings, *, transport=None, clock=time.monotonic):
        self.settings, self.transport, self.clock = settings, transport, clock
        self.last_start = None
        self.last_busy = clock()
        self.stopped = False

    async def _call(self, action):
        async with httpx.AsyncClient(transport=self.transport, timeout=30) as http:
            response = await http.post(f'{API}/{self.settings.pod_id}/{action}',
                                       headers={'Authorization': f'Bearer {self.settings.api_key}'})
            response.raise_for_status()

    async def ensure_started(self):
        """Start the Pod at most once per retry window; RunPod may lack free GPUs on the host."""
        self.last_busy = self.clock()
        if self.last_start is not None and self.clock() - self.last_start < self.settings.start_retry_seconds:
            return False
        self.last_start = self.clock()
        try:
            await self._call('start')
        except httpx.HTTPError as exc:
            logging.warning('GPU Pod start failed (%s); jobs stay queued', type(exc).__name__)
            return False
        self.stopped = False
        logging.info('GPU Pod start requested')
        return True

    def mark_busy(self):
        self.last_busy = self.clock()

    async def stop_if_idle(self):
        """Stop the Pod once the queue has been empty for idle_stop_seconds."""
        if self.stopped or self.clock() - self.last_busy < self.settings.idle_stop_seconds:
            return False
        try:
            await self._call('stop')
        except httpx.HTTPError as exc:
            logging.warning('GPU Pod stop failed (%s)', type(exc).__name__)
            return False
        logging.info('GPU Pod stop requested after idle period')
        # Avoid re-sending stop every loop; the next start clears this.
        self.stopped = True
        self.last_start = None
        return True
