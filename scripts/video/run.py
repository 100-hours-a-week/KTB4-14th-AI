"""영상 환경 점검·단일 클립 검증·최종 영상 생성·측정 기록 집계 명령을 제공한다."""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import statistics

import httpx
from pydantic import ValidationError

from ai_service.clients.comfyui import ComfyUI
from ai_service.clients.runpod import EndpointResolver
from ai_service.schemas.video import ClipRequest, VideoRequest, DEFAULT_MOTION
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.workflow import build_workflow, validate_workflow


def summarize(workspace: Path) -> dict:
    reports = [json.loads(path.read_text()) for path in sorted(workspace.glob("*/report.json"))]
    groups = {}
    for report in reports:
        key = (report["execution_mode"], report["test_kind"], report["target_duration_seconds"])
        groups.setdefault(key, []).append(report)
    comparisons = []
    for (mode, kind, duration), rows in sorted(groups.items()):
        completed = [r for r in rows if r["status"] in {"completed", "mock_completed"}]
        elapsed = sorted(r["total_seconds"] for r in completed)
        costs = [r["estimated_request_window_cost_usd"] for r in rows if r.get("estimated_request_window_cost_usd") is not None]
        comparisons.append({
            "execution_mode": mode, "test_kind": kind, "duration_seconds": duration,
            "attempts": len(rows), "completed": len(completed), "failed": len(rows)-len(completed),
            "median_total_seconds": statistics.median(elapsed) if elapsed else None,
            "max_total_seconds": max(elapsed) if elapsed else None,
            "sum_estimated_request_cost_usd_including_failed_attempts": sum(costs) if costs else None,
            "estimated_cost_per_completed_video_including_failures": sum(costs)/len(completed) if costs and completed else None,
        })
    return {
        "comparisons": comparisons,
        "planning_constraints": {
            "budget_usd": 269, "target_videos": 500, "target_date_kst": "2026-11-17",
            "hourly_usd_reference": 1.11, "max_cost_per_video_usd_before_overhead": 269/500,
            "max_reserved_minutes_per_video_before_overhead": 269/500/1.11*60,
            "total_reserved_hours_before_overhead": 269/1.11,
        },
        "production_500_feasibility": "not_validated",
        "limitations": "Mock times are CPU/API/encoding only. Live request-window costs exclude setup, idle, orphan GPU work after failure, persistent disk and billing differences. Visual quality, retries, throughput and actual Pod invoices must be measured before a 500-video decision.",
    }


async def execute(args):
    settings = VideoSettings.from_env()
    if args.workspace:
        settings = replace(settings, workspace=args.workspace)
    if args.mock:
        from scripts.video.mock_comfyui import MockComfyUI
        mock = MockComfyUI(settings.ffmpeg_bin)
        settings = replace(settings, endpoint_mode="static", comfyui_url="http://mock-comfyui", poll_interval=0.001)
        transport = httpx.MockTransport(mock.handle)
    else:
        transport = None
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http:
        if args.command == "check":
            endpoint = await EndpointResolver(http, settings).resolve()
            client = ComfyUI(http, endpoint, settings)
            runtime = await client.runtime_info(require_cuda=not args.mock)
            info = await client.object_info()
            validate_workflow(build_workflow(
                settings, start="start.png", end="end.png", frames=81, fps=16.2,
                prompt=DEFAULT_MOTION, seed=0, prefix="audigo/preflight",
            ), info)
            print(json.dumps({"status": "mock_preflight_passed" if args.mock else "preflight_passed", "pod_id": endpoint.pod_id,
                              "gpu_inference_executed": False, "runtime": runtime}))
            return
        model = ClipRequest if args.command == "smoke" else VideoRequest
        request = model(photos=args.photos, duration_seconds=args.duration, prompt=args.prompt, seed=args.seed)
        from ai_service.services.video.pipeline import VideoPipeline
        result = await VideoPipeline(http, settings, execution_mode="mock" if args.mock else "live").generate(request)
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="AUDIGO Wan2.2 FLF2V pipeline; Mock output is synthetic, never AI-generated.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "smoke", "generate"):
        sub = commands.add_parser(command)
        sub.add_argument("--mock", action="store_true", help="Synthetic HTTP/MP4 test double; no GPU/AI inference")
        sub.add_argument("--workspace", type=Path)
        if command != "check":
            sub.add_argument("--photos", type=Path, nargs="+", required=True, help="Local files in explicit travel order")
            sub.add_argument("--duration", type=int, default=5 if command == "smoke" else 10)
            sub.add_argument("--prompt", default=DEFAULT_MOTION)
            sub.add_argument("--seed", type=int, default=0)
    summary = commands.add_parser("summarize")
    summary.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "summarize":
            workspace = args.workspace or VideoSettings.from_env().workspace
            print(json.dumps(summarize(workspace), ensure_ascii=False, indent=2))
        else:
            asyncio.run(execute(args))
    except (VideoError, ValidationError, ValueError, TimeoutError) as exc:
        # 사용자 데이터나 비밀값이 포함될 수 있는 예외 본문·검증 입력·주소는 출력하지 않는다.
        reason = exc.reason if isinstance(exc, VideoError) else "video_timeout" if isinstance(exc, TimeoutError) else "invalid_request_or_configuration"
        parser.exit(1, json.dumps({"status": "failed", "reason": reason}) + "\n")


if __name__ == "__main__":
    main()
