"""RunPod에서 실행 중인 GPU Pod를 찾고 ComfyUI 접속 주소와 준비 상태를 관리한다."""
import asyncio
from dataclasses import dataclass
import re
import time

import httpx

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError


@dataclass(frozen=True)
class GpuEndpoint:
    url: str
    pod_id: str | None = None


class EndpointResolver:
    def __init__(self, http: httpx.AsyncClient, settings: VideoSettings):
        self.http, self.settings = http, settings
        self._cached: GpuEndpoint | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self, endpoint: GpuEndpoint):
        if self._cached == endpoint:
            self._cached = None
            self._expires = 0.0

    async def _ready(self, endpoint: GpuEndpoint) -> bool:
        headers = {"Authorization": f"Bearer {self.settings.comfyui_token}"} if self.settings.comfyui_token else {}
        try:
            response = await self.http.get(endpoint.url + "/system_stats", headers=headers, timeout=self.settings.request_timeout)
            response.raise_for_status()
            stats = response.json()
            return isinstance(stats, dict) and isinstance(stats.get("system"), dict) and isinstance(stats.get("devices"), list)
        except (httpx.HTTPError, ValueError):
            return False

    async def resolve(self) -> GpuEndpoint:
        # 작업마다 주소를 한 번 선택하고 업로드·생성·상태 조회·다운로드를 같은 Pod에서 처리한다.
        async with self._lock:
            if self._cached and time.monotonic() < self._expires:
                if await self._ready(self._cached):
                    return self._cached
                self._cached = None
            if self.settings.endpoint_mode == "static":
                endpoint = GpuEndpoint(self.settings.comfyui_url.rstrip("/"))
                if not await self._ready(endpoint):
                    raise VideoError("comfyui_not_ready")
                return endpoint
            if not self.settings.runpod_api_key:
                raise VideoError("runpod_key_missing")
            try:
                response = await self.http.get(
                    "https://rest.runpod.io/v1/pods",
                    headers={"Authorization": f"Bearer {self.settings.runpod_api_key}"},
                    params={"desiredStatus": "RUNNING", "computeType": "GPU"},
                    timeout=self.settings.request_timeout,
                )
                response.raise_for_status()
                pods = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise VideoError("runpod_discovery_unavailable") from exc
            if not isinstance(pods, list):
                raise VideoError("runpod_response_invalid")
            candidates = []
            for pod in pods:
                if not isinstance(pod, dict):
                    continue
                pod_id, name = pod.get("id"), pod.get("name", "")
                if (not isinstance(pod_id, str) or not re.fullmatch(r"[a-zA-Z0-9]+", pod_id)
                        or not isinstance(name, str) or not name.startswith(self.settings.runpod_prefix)
                        or pod.get("desiredStatus") != "RUNNING"
                        or f"{self.settings.runpod_port}/http" not in (pod.get("ports") or [])
                        or (self.settings.runpod_pod_id and pod_id != self.settings.runpod_pod_id)):
                    continue
                candidates.append(GpuEndpoint(f"https://{pod_id}-{self.settings.runpod_port}.proxy.runpod.net", pod_id))
            # 후보를 일정한 순서로 살피며 실행 상태뿐 아니라 실제 ComfyUI 준비 여부도 확인한다.
            for endpoint in sorted(candidates, key=lambda e: e.pod_id):
                if await self._ready(endpoint):
                    self._cached = endpoint
                    self._expires = time.monotonic() + self.settings.discovery_ttl
                    return endpoint
            raise VideoError("runpod_no_ready_pod", candidates=len(candidates))
