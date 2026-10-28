"""ComfyUI의 이미지 업로드·영상 생성 요청·상태 조회·결과 다운로드를 처리한다."""
import asyncio
from pathlib import Path, PurePosixPath
import re

import httpx

from ai_service.clients.runpod import GpuEndpoint
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError


def asset_reference(asset: dict, *, expected_type: str) -> dict:
    if not isinstance(asset, dict):
        raise VideoError("comfyui_asset_invalid")
    name, subfolder = asset.get("filename", asset.get("name")), asset.get("subfolder", "")
    if (not isinstance(name, str) or not name or PurePosixPath(name).name != name
            or "\\" in name or name in {".", ".."}
            or not isinstance(subfolder, str) or "\\" in subfolder
            or PurePosixPath(subfolder).is_absolute() or ".." in PurePosixPath(subfolder).parts
            or asset.get("type") != expected_type):
        raise VideoError("comfyui_asset_invalid")
    return {"filename": name, "subfolder": subfolder, "type": expected_type}


class ComfyUI:
    def __init__(self, http: httpx.AsyncClient, endpoint: GpuEndpoint, settings: VideoSettings):
        self.http, self.endpoint, self.settings = http, endpoint, settings
        self.headers = {"Authorization": f"Bearer {settings.comfyui_token}"} if settings.comfyui_token else {}

    async def _json(self, method, route, **kwargs):
        try:
            response = await self.http.request(
                method, self.endpoint.url + route, headers=self.headers,
                timeout=self.settings.request_timeout, **kwargs,
            )
            if response.status_code == 400 and route == "/prompt":
                raise VideoError("comfyui_prompt_rejected")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # 작업 등록 후 응답만 유실될 수 있으므로 재전송에 따른 중복 GPU 과금을 방지한다.
            reason = "comfyui_submission_uncertain" if route == "/prompt" else "comfyui_http_failed"
            raise VideoError(reason, operation=route.split("/")[1]) from exc

    async def object_info(self) -> dict:
        info = await self._json("GET", "/object_info")
        if not isinstance(info, dict):
            raise VideoError("comfyui_schema_invalid")
        return info

    async def runtime_info(self, *, require_cuda=False) -> dict:
        stats = await self._json("GET", "/system_stats")
        # 토큰이 포함될 수 있는 실행 인자와 시스템 응답 전체는 보관하지 않는다.
        if (not isinstance(stats, dict) or not isinstance(stats.get("system"), dict)
                or not isinstance(stats.get("devices"), list)
                or not all(isinstance(d, dict) for d in stats["devices"])):
            raise VideoError("comfyui_stats_invalid")
        if require_cuda and not any(d.get("type") == "cuda" for d in stats["devices"]):
            raise VideoError("comfyui_gpu_unavailable")
        system = stats.get("system", {})
        return {
            "system": {key: system.get(key) for key in ("comfyui_version", "python_version", "pytorch_version")},
            "devices": [{key: device.get(key) for key in ("name", "type", "vram_total")}
                        for device in stats.get("devices", [])],
        }

    async def upload(self, path: Path, subfolder: str) -> str:
        with path.open("rb") as image:
            data = await self._json(
                "POST", "/upload/image", files={"image": (path.name, image, "image/png")},
                data={"type": "input", "subfolder": subfolder, "overwrite": "false"},
            )
        asset = asset_reference(data, expected_type="input")
        # 중복 파일명을 서버가 바꿀 수 있으므로 응답으로 받은 파일명과 하위 경로를 사용한다.
        return str(PurePosixPath(asset["subfolder"]) / asset["filename"])

    async def submit(self, workflow: dict, client_id: str) -> str:
        data = await self._json("POST", "/prompt", json={"prompt": workflow, "client_id": client_id})
        if not isinstance(data, dict) or data.get("error") or data.get("node_errors"):
            raise VideoError("comfyui_prompt_rejected")
        prompt_id = data.get("prompt_id")
        if not isinstance(prompt_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", prompt_id):
            raise VideoError("comfyui_submission_uncertain")
        return prompt_id

    async def wait(self, prompt_id: str, output_node: str, on_state=None) -> dict:
        previous = None
        async with asyncio.timeout(self.settings.prompt_timeout):
            failures = 0
            while True:
                try:
                    data = await self._json("GET", f"/history/{prompt_id}")
                    failures = 0
                except VideoError:
                    failures += 1
                    if failures >= 3:
                        raise
                    await asyncio.sleep(self.settings.poll_interval)
                    continue
                if not isinstance(data, dict):
                    raise VideoError("comfyui_history_invalid")
                history = data.get(prompt_id)
                if history is not None:
                    status = history.get("status", {})
                    messages = status.get("messages", [])
                    if (status.get("status_str") == "error"
                            or any(m[0] in {"execution_error", "execution_interrupted"} for m in messages)):
                        # 예외 본문에 비공개 프롬프트나 경로가 포함될 수 있으므로 이벤트 유형만 확인한다.
                        raise VideoError("comfyui_execution_failed", prompt_id=prompt_id)
                    if status.get("completed") is True:
                        # ComfyUI 0.35.0의 SaveVideo는 PreviewVideo 결과를 images 항목으로 반환한다.
                        outputs = history.get("outputs", {}).get(output_node, {}).get("images", [])
                        videos = [x for x in outputs if isinstance(x, dict) and str(x.get("filename", "")).lower().endswith(".mp4")]
                        if len(videos) != 1:
                            raise VideoError("comfyui_video_missing", prompt_id=prompt_id)
                        return {"asset": asset_reference(videos[0], expected_type="output"),
                                "execution_seconds": self._execution_seconds(messages)}
                queue = await self._json("GET", "/queue")
                state = "unknown"
                for key, label in (("queue_running", "running"), ("queue_pending", "queued")):
                    if any(len(item) > 1 and item[1] == prompt_id for item in queue.get(key, [])):
                        state = label
                if state != previous and on_state:
                    on_state(state)
                previous = state
                await asyncio.sleep(self.settings.poll_interval)

    @staticmethod
    def _execution_seconds(messages):
        times = {entry[0]: entry[1].get("timestamp") for entry in messages if len(entry) == 2 and isinstance(entry[1], dict)}
        start, end = times.get("execution_start"), times.get("execution_success")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
            return (end - start) / 1000
        return None

    async def download(self, asset: dict, destination: Path):
        temporary = destination.with_suffix(".part")
        try:
            async with self.http.stream(
                "GET", self.endpoint.url + "/view", params=asset, headers=self.headers,
                timeout=self.settings.request_timeout,
            ) as response:
                response.raise_for_status()
                received = 0
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > self.settings.max_clip_bytes:
                            raise VideoError("comfyui_clip_too_large")
                        output.write(chunk)
                if not received:
                    raise VideoError("comfyui_clip_empty")
            temporary.replace(destination)
        except httpx.HTTPError as exc:
            raise VideoError("comfyui_download_failed") from exc
        finally:
            temporary.unlink(missing_ok=True)
