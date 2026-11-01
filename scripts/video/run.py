"""영상 환경 점검·단일 클립 검증·최종 영상 생성·측정 기록 집계 명령을 제공한다."""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import statistics
import shutil
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ai_service.clients.comfyui import ComfyUI
from ai_service.clients.runpod import EndpointResolver
from ai_service.schemas.video import ClipRequest, VideoRequest, MemoryVideoRequest, MusicSelection, DEFAULT_MOTION
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.workflow import build_workflow, model_availability, validate_workflow
from ai_service.services.video.budget import production_budget


def summarize(workspace: Path) -> dict:
    reports = [json.loads(path.read_text()) for path in sorted(workspace.glob("*/report.json"))]
    groups = {}
    for report in reports:
        key = (report["execution_mode"], report["test_kind"], report["target_duration_seconds"],
               report.get('generation_profile', 'standard'), report.get('story_mode', 'TRANSITION'))
        groups.setdefault(key, []).append(report)
    comparisons = []
    for (mode, kind, duration, profile, story_mode), rows in sorted(groups.items()):
        completed = [r for r in rows if r["status"] in {"completed", "mock_completed", "selection_completed"}]
        elapsed = sorted(r["total_seconds"] for r in completed)
        costs = [r["estimated_request_window_cost_usd"] for r in rows if r.get("estimated_request_window_cost_usd") is not None]
        comparisons.append({
            "execution_mode": mode, "test_kind": kind, "duration_seconds": duration,
            "generation_profile": profile,
            'story_mode': story_mode,
            "attempts": len(rows), "completed": len(completed), "failed": len(rows)-len(completed),
            "median_total_seconds": statistics.median(elapsed) if elapsed else None,
            "max_total_seconds": max(elapsed) if elapsed else None,
            "sum_estimated_request_cost_usd_including_failed_attempts": sum(costs) if costs else None,
            "estimated_cost_per_completed_video_including_failures": sum(costs)/len(completed) if costs and completed else None,
        })
    return {
        "comparisons": comparisons,
        "planning_constraints": production_budget(),
        "production_500_feasibility": "not_validated",
        "limitations": "Mock times are CPU/API/encoding only. Live request-window costs exclude setup, idle, orphan GPU work after failure, persistent disk and billing differences. Visual quality, retries, throughput and actual Pod invoices must be measured before a 500-video decision.",
    }


async def execute(args):
    settings = VideoSettings.from_env()
    if args.profile:
        settings = replace(settings, generation_profile=args.profile)
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
            graph = build_workflow(
                settings, start="start.png", end="end.png", frames=81, fps=16.2,
                prompt=DEFAULT_MOTION, seed=0, prefix="audigo/preflight",
            )
            models = model_availability(graph, info)
            try:
                validate_workflow(graph, info)
            except VideoError as exc:
                exc.detail['models'] = models
                raise
            print(json.dumps({"status": "mock_preflight_passed" if args.mock else "preflight_passed", "pod_id": endpoint.pod_id,
                              "gpu_inference_executed": False, "runtime": runtime, "models": models,
                              "generation_profile": settings.generation_profile}))
            return
        if args.command == 'memory':
            from ai_service.services.video.selection import ordered_photo_files
            request = MemoryVideoRequest(
                photos=ordered_photo_files(args.input_dir) if args.input_dir else args.photos,
                max_selected_photos=args.max_photos, transition_seconds=args.transition_seconds,
                max_duration_seconds=args.max_duration, preview_only=args.preview_only,
                backend=args.backend,
                duration_seconds=args.duration,reference_strength=args.reference_strength,
                memory_style=args.memory_style, memory_context=args.memory_context,
                photo_fit=args.photo_fit, prompt=args.prompt, seed=args.seed,
                music=MusicSelection(preset=args.music, file=args.music_file,
                                     volume=args.music_volume, start_seconds=args.music_start))
        else:
            model = ClipRequest if args.command == "smoke" else VideoRequest
            request = model(
                photos=args.photos, duration_seconds=args.duration, prompt=args.prompt, seed=args.seed,
                memory_style=args.memory_style, memory_context=args.memory_context,
                photo_fit=args.photo_fit, quality_policy=args.quality_policy,
                story_mode=args.story_mode,
                music=MusicSelection(preset=args.music, file=args.music_file,
                                     volume=args.music_volume, start_seconds=args.music_start),
            )
        from ai_service.services.video.pipeline import VideoPipeline
        result = await VideoPipeline(http, settings, execution_mode="mock" if args.mock else "live").generate(request)
        print(json.dumps(result, ensure_ascii=False, indent=2))


async def remix(args):
    """음악·색감 변경은 기존 생성 클립으로 후처리하여 GPU를 다시 사용하지 않는다."""
    from ai_service.services.video.media import Media
    settings = VideoSettings.from_env()
    if args.workspace:
        settings = replace(settings, workspace=args.workspace)
    if not args.video.is_file():
        raise VideoError('video_missing')
    started = time.perf_counter()
    media = Media(settings)
    original = await media.probe(args.video)
    duration = original['duration_seconds']
    if not 0 < duration <= 180:
        raise VideoError('video_duration_invalid')
    folder = settings.workspace.resolve() / uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    clip = folder / 'clip-0000.mp4'
    shutil.copy2(args.video, clip)
    selection = MusicSelection(preset=args.music, file=args.music_file,
                               volume=args.music_volume, start_seconds=args.music_start)
    audio, music = await media.prepare_music(selection, folder, duration, args.memory_style, args.memory_context)
    output, info = await media.finalize([clip], folder, duration, audio=audio,
                                      volume=selection.volume, memory_style=args.memory_style)
    result = {'status': 'completed', 'execution_mode': 'postprocess', 'test_kind': 'music_remix',
              'gpu_inference_executed': False, 'target_duration_seconds': duration,
              'memory_style': args.memory_style, 'music': music, 'media': info,
              'output_file': str(output), 'total_seconds': round(time.perf_counter() - started, 3)}
    (folder / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def memory_arguments(parser):
    parser.add_argument('--memory-style', choices=('WARM', 'LIVELY', 'CINEMATIC'), default='WARM')
    parser.add_argument('--memory-context', default='', help='추억 정보. 음악 AUTO와 영상 연출에 반영')
    parser.add_argument('--music', choices=('AUTO', 'WARM', 'BRIGHT', 'CALM', 'NONE'), default='AUTO')
    parser.add_argument('--music-file', type=Path, help='사용자가 선택한 서버/로컬 음원 파일. 프리셋보다 우선')
    parser.add_argument('--music-volume', type=float, default=0.65)
    parser.add_argument('--music-start', type=float, default=0)


def main():
    parser = argparse.ArgumentParser(description="AUDIGO Wan2.2 FLF2V pipeline; Mock output is synthetic, never AI-generated.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "smoke", "generate"):
        sub = commands.add_parser(command)
        sub.add_argument("--mock", action="store_true", help="Synthetic HTTP/MP4 test double; no GPU/AI inference")
        sub.add_argument("--workspace", type=Path)
        sub.add_argument('--profile', choices=('standard', 'fast'),
                         help='standard: 20 steps / CFG 4; fast: official LightX2V LoRAs / 4 steps / CFG 1, same 720p output')
        if command != "check":
            sub.add_argument("--photos", type=Path, nargs="+", required=True, help="Local files in explicit travel order")
            sub.add_argument("--duration", type=int, default=5 if command == "smoke" else 10)
            sub.add_argument("--prompt", default=DEFAULT_MOTION)
            sub.add_argument("--seed", type=int, default=0)
            memory_arguments(sub)
            sub.add_argument('--photo-fit', choices=('blurred_background', 'letterbox'), default='blurred_background')
            sub.add_argument('--quality-policy', choices=('WARN', 'REJECT'), default='WARN')
            sub.add_argument('--story-mode', choices=('MONTAGE', 'TRANSITION', 'PHOTO_MOTION'), default='TRANSITION',
                             help='PHOTO_MOTION: 원본 사진 줌·이동·컷 편집(GPU 없음); MONTAGE: 사진별 AI 숏; TRANSITION: 연속 변환')
    memory = commands.add_parser('memory', help='OpenCV → 장소/행동 분석 → 재방문 시점의 단일 30초 AI 영상')
    source = memory.add_mutually_exclusive_group(required=True)
    source.add_argument('--input-dir', type=Path, help='폴더 전체, 촬영 EXIF 시간순. 미지원 파일은 제외 기록')
    source.add_argument('--photos', type=Path, nargs='+', help='명시한 여행 순서의 사진 목록')
    memory.add_argument('--workspace', type=Path)
    memory.add_argument('--profile', choices=('standard', 'fast'), default='fast')
    memory.add_argument('--mock', action='store_true', help='선별은 실제 OpenCV, GPU 결과는 합성 테스트 패턴')
    memory.add_argument('--preview-only', action='store_true', help='선별·FLF 계획만 저장, GPU 호출 없음')
    memory.add_argument('--backend',choices=('ltx23','ltx23_film','wan_segments'),default='ltx23_film',help='기본: 서로 다른 장면 자동 선택·검수·편집. ltx23은 이전 단일 생성 경로')
    memory.add_argument('--max-photos', type=int, default=12, help='이전 ltx23/wan_segments 참고 사진 상한. 기본 film은 가용 장면으로 자동 결정')
    memory.add_argument('--duration',type=int,default=30,help='실제 생성할 목표 길이, 기본 30초')
    memory.add_argument('--reference-strength',type=float,default=0.7,help='사진 구도 고정이 아닌 장소 참고 강도')
    memory.add_argument('--transition-seconds', type=int, default=5, help='이전 wan_segments 경로의 구간 길이')
    memory.add_argument('--max-duration', type=int, default=30, help='현재 자체 실행 검증 길이 상한, 30초')
    memory.add_argument('--photo-fit', choices=('cinematic_crop','blurred_background','letterbox'), default='cinematic_crop')
    memory.add_argument('--prompt', default=DEFAULT_MOTION)
    memory.add_argument('--seed', type=int, default=0)
    memory_arguments(memory)
    summary = commands.add_parser("summarize")
    summary.add_argument("--workspace", type=Path)
    catalog = commands.add_parser('music-list')
    catalog.add_argument('--preview-dir', type=Path, help='세 가지 자체 합성 프리셋의 12초 미리듣기 WAV 저장')
    editor = commands.add_parser('remix', help='기존 영상에 선택 음악·색감 적용. GPU 재생성 없음')
    editor.add_argument('--video', type=Path, required=True)
    editor.add_argument('--workspace', type=Path)
    memory_arguments(editor)
    args = parser.parse_args()
    try:
        if args.command == 'music-list':
            from ai_service.services.video.music import MUSIC_CATALOG, render_preset
            choices = [dict(item) for item in MUSIC_CATALOG]
            if args.preview_dir:
                args.preview_dir.mkdir(parents=True, exist_ok=True)
                for item in choices:
                    if item['id'] in ('WARM', 'BRIGHT', 'CALM'):
                        path = args.preview_dir / (item['id'].lower() + '.wav')
                        render_preset(path, item['id'], 12)
                        item['preview_file'] = str(path.resolve())
            print(json.dumps({'music_choices': choices, 'user_file_supported': True}, ensure_ascii=False, indent=2))
        elif args.command == 'remix':
            asyncio.run(remix(args))
        elif args.command == "summarize":
            workspace = args.workspace or VideoSettings.from_env().workspace
            print(json.dumps(summarize(workspace), ensure_ascii=False, indent=2))
        else:
            asyncio.run(execute(args))
    except (VideoError, ValidationError, ValueError, TimeoutError) as exc:
        # 사용자 데이터나 비밀값이 포함될 수 있는 예외 본문·검증 입력·주소는 출력하지 않는다.
        reason = exc.reason if isinstance(exc, VideoError) else "video_timeout" if isinstance(exc, TimeoutError) else "invalid_request_or_configuration"
        result = {"status": "failed", "reason": reason}
        if isinstance(exc, VideoError):
            # 로컬 워크플로의 노드 정보와 모델 유무만 표시한다.
            detail = {key: exc.detail[key] for key in ('node', 'field', 'models') if key in exc.detail}
            if detail:
                result['detail'] = detail
        parser.exit(1, json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
