"""사진 전처리부터 Wan2.2 생성·후처리·시간 및 실패 기록까지 영상 제작 전 과정을 실행한다."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

from ai_service.clients.comfyui import ComfyUI
from ai_service.clients.runpod import EndpointResolver
from ai_service.core.diagnostics import record
from ai_service.schemas.video import ClipRequest, VideoRequest
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.images import prepare_photo
from ai_service.services.video.media import Media
from ai_service.services.video.planning import plan_clips
from ai_service.services.video.workflow import build_workflow, validate_workflow
from ai_service.services.video.workflow import WORKFLOWS


class VideoPipeline:
    def __init__(self, http: httpx.AsyncClient, settings: VideoSettings, *, execution_mode="live", resolver=None):
        if execution_mode not in {"live", "mock"}:
            raise ValueError("execution_mode must be live or mock")
        self.http, self.settings, self.execution_mode = http, settings, execution_mode
        self.resolver = resolver or EndpointResolver(http, settings)
        self.media = Media(settings)

    async def generate(self, request: VideoRequest | ClipRequest) -> dict:
        smoke = isinstance(request, ClipRequest)
        started, reserved_started = time.perf_counter(), None
        job_id = uuid4().hex
        folder = self.settings.workspace.resolve() / job_id
        folder.mkdir(parents=True, exist_ok=False)
        manifest = {
            "job_id": job_id, "execution_mode": self.execution_mode, "test_kind": "single_clip_smoke" if smoke else "final_video",
            "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
            "target_duration_seconds": request.duration_seconds, "photo_count": len(request.photos),
            "seed": request.seed, "pod_id": None, "clips": [], "events": [],
            "workflow_provenance": json.loads((WORKFLOWS / "provenance.json").read_text()),
            "gpu_billed_seconds": None, "actual_cost_usd": None,
            "cost_note": "Request-window estimate only; Pod idle/setup/storage/retries and actual billing must be accounted separately.",
            "hourly_usd": self.settings.hourly_usd,
        }

        def save():
            temporary = folder / "report.tmp"
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(folder / "report.json")

        def event(stage, **fields):
            manifest["events"].append({"stage": stage, "elapsed_seconds": round(time.perf_counter() - started, 3), **fields})
            save()
            record("video_progress", job_id=job_id, stage=stage, execution_mode=self.execution_mode, **fields)

        endpoint = None
        try:
            async with asyncio.timeout(self.settings.job_timeout):
                event("validating")
                plans = plan_clips(len(request.photos), request.duration_seconds, smoke=smoke)
                photos = []
                for index, source in enumerate(request.photos):
                    destination = folder / f"photo-{index:04d}.png"
                    info = await asyncio.to_thread(prepare_photo, source, destination, self.settings.max_input_bytes)
                    photos.append(destination)
                    manifest.setdefault("preprocessing", []).append(info)
                event("discovering")
                endpoint = await self.resolver.resolve()
                reserved_started = time.perf_counter()
                manifest["pod_id"] = endpoint.pod_id
                client = ComfyUI(self.http, endpoint, self.settings)
                manifest["runtime"] = await client.runtime_info(require_cuda=self.execution_mode == "live")
                info = await client.object_info()
                first = plans[0]
                # 사진 업로드와 작업 제출 전에 서버의 노드 입력 형식과 설치된 모델 목록을 확인한다.
                validate_workflow(build_workflow(
                    self.settings, start="start.png", end="end.png", frames=first.frames, fps=first.fps,
                    prompt=request.prompt, seed=request.seed, prefix=f"audigo/{job_id}/clip-0000",
                ), info)
                event("uploading")
                uploaded = [await client.upload(path, f"audigo/{job_id}") for path in photos]
                clips = []
                for index, plan in enumerate(plans):
                    entry = {"index": index, **asdict(plan), "status": "submitting", "prompt_id": None}
                    manifest["clips"].append(entry)
                    event("generating", clip_index=index, clip_count=len(plans))
                    workflow = build_workflow(
                        self.settings, start=uploaded[plan.start_index], end=uploaded[plan.end_index],
                        frames=plan.frames, fps=plan.fps, prompt=request.prompt,
                        seed=(request.seed + index) % (2**63), prefix=f"audigo/{job_id}/clip-{index:04d}",
                    )
                    validate_workflow(workflow, info)
                    (folder / f"workflow-{index:04d}.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=2))
                    prompt_started = time.perf_counter()
                    try:
                        prompt_id = await client.submit(workflow, job_id)
                        entry.update(prompt_id=prompt_id, status="submitted")
                        event("submitted", clip_index=index, prompt_id=prompt_id)

                        def state_changed(state):
                            entry["status"] = state
                            event("comfyui_" + state, clip_index=index, prompt_id=prompt_id)

                        result = await client.wait(prompt_id, "83", state_changed)
                        entry["provider_execution_seconds"] = result["execution_seconds"]
                        entry["asset"] = result["asset"]
                        entry["status"] = "downloading"
                        event("downloading", clip_index=index, prompt_id=prompt_id)
                        destination = folder / f"clip-{index:04d}.mp4"
                        await client.download(result["asset"], destination)
                        entry["media"] = await self.media.check_clip(destination, plan.duration_seconds)
                        entry["status"] = "completed"
                        clips.append(destination)
                    finally:
                        entry["request_to_clip_seconds"] = round(time.perf_counter() - prompt_started, 3)
                        save()
                event("encoding")
                output, media = await self.media.finalize(clips, folder, request.duration_seconds)
                manifest.update(output_file=str(output), media=media)
                event("encoded")
        except BaseException as exc:
            reason = (exc.reason if isinstance(exc, VideoError) else "video_timeout" if isinstance(exc, TimeoutError)
                      else "video_cancelled" if isinstance(exc, asyncio.CancelledError) else "video_internal_error")
            manifest.update(status="failed", failure_reason=reason, failure_type=type(exc).__name__)
            if isinstance(exc, VideoError):
                manifest["failure_detail"] = exc.detail
            # 다른 작업도 중단시키는 전체 중단 API나 제출 여부가 불명확한 요청의 재전송은 사용하지 않는다.
            manifest["gpu_work_may_still_be_running"] = any(c["status"] != "completed" for c in manifest["clips"])
            event("failed", reason=reason)
            if endpoint:
                self.resolver.invalidate(endpoint)
            raise
        finally:
            ended = time.perf_counter()
            manifest["total_seconds"] = round(ended - started, 3)
            manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            reserved = ended - reserved_started if reserved_started is not None and self.execution_mode == "live" else None
            manifest["estimated_pod_reserved_seconds"] = round(reserved, 3) if reserved is not None else None
            manifest["estimated_request_window_cost_usd"] = reserved / 3600 * self.settings.hourly_usd if reserved is not None else None
            # 최종 성공 상태는 처리 시간·비용 필드와 함께 기록한다.
            if manifest["status"] == "running":
                manifest["status"] = "mock_completed" if self.execution_mode == "mock" else "completed"
                event("completed")
            save()
            record("video_finished", job_id=job_id, status=manifest["status"], execution_mode=self.execution_mode,
                   total_seconds=manifest["total_seconds"], estimated_cost_usd=manifest["estimated_request_window_cost_usd"])
        return manifest
