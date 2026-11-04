"""사진 전처리부터 Wan2.2 생성·후처리·시간 및 실패 기록까지 영상 제작 전 과정을 실행한다."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time
from uuid import uuid4

import httpx

from ai_service.clients.comfyui import ComfyUI
from ai_service.clients.runpod import EndpointResolver
from ai_service.core.diagnostics import record
from ai_service.schemas.video import ClipRequest, VideoRequest, MemoryVideoRequest
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.images import prepare_photo
from ai_service.services.video.media import Media
from ai_service.services.video.planning import plan_clips
from ai_service.services.video.workflow import build_workflow, validate_workflow
from ai_service.services.video.workflow import WORKFLOWS
from ai_service.prompts.video import memory_prompt
from ai_service.services.video.selection import select_photos
from ai_service.services.video.ltx import build_ltx_workflow, keyframe_plan, single_video_prompt, LTX_PROVENANCE, story_durations
from ai_service.services.video.storyboard import plan_travel_story, validate_storyboard, scene_candidates


class VideoPipeline:
    def __init__(self, http: httpx.AsyncClient, settings: VideoSettings, *, execution_mode="live", resolver=None):
        if execution_mode not in {"live", "mock"}:
            raise ValueError("execution_mode must be live or mock")
        self.http, self.settings, self.execution_mode = http, settings, execution_mode
        self.resolver = resolver or EndpointResolver(http, settings)
        self.media = Media(settings)

    async def generate(self, request: VideoRequest | ClipRequest | MemoryVideoRequest) -> dict:
        if isinstance(request, MemoryVideoRequest) and request.backend == 'ltx23_film':
            from ai_service.services.video.film import generate_diverse_film
            if self.execution_mode != 'live':
                raise VideoError('diverse_film_requires_live_or_injected_test_providers')
            return await generate_diverse_film(self.http, self.settings, request, self.settings.workspace.resolve() / uuid4().hex, resolver=self.resolver)
        smoke = isinstance(request, ClipRequest)
        automatic = isinstance(request, MemoryVideoRequest)
        single_ltx = automatic and request.backend == 'ltx23'
        scene_pairs = False
        scene_durations = None
        duration = None if automatic else request.duration_seconds
        started, reserved_started = time.perf_counter(), None
        job_id = uuid4().hex
        folder = self.settings.workspace.resolve() / job_id
        folder.mkdir(parents=True, exist_ok=False)
        manifest = {
            "job_id": job_id, "execution_mode": self.execution_mode, "test_kind": "single_clip_smoke" if smoke else "final_video",
            "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
            "target_duration_seconds": duration, "photo_count": len(request.photos),
            "seed": request.seed, "pod_id": None, "clips": [], "events": [],
            "generation_profile": self.settings.generation_profile,
            'memory_style': request.memory_style,
            'photo_fit': request.photo_fit,
            'quality_policy': request.quality_policy,
            'story_mode': request.story_mode,
            "latency_target_seconds": 300 if smoke and self.settings.generation_profile == 'fast' else None,
            "workflow_provenance": json.loads((WORKFLOWS / "provenance.json").read_text()),
            "gpu_billed_seconds": None, "actual_cost_usd": None,
            "cost_note": "Request-window estimate only; Pod idle/setup/storage/retries and actual billing must be accounted separately.",
            "hourly_usd": self.settings.hourly_usd,
        }
        local_motion = request.story_mode == 'PHOTO_MOTION'
        if local_motion:
            manifest.update(execution_mode='photo_motion', generation_profile='photo_motion',
                            gpu_inference_executed=False, latency_target_seconds=300 if smoke else None)
        if automatic:
            manifest['input_files'] = [str(path) for path in request.photos]
            manifest['gpu_inference_executed'] = False
            if request.preview_only:
                manifest['execution_mode'] = 'selection_preview'
            manifest['backend'] = request.backend
            if single_ltx:
                manifest.update(generation_profile='ltx23_distilled_8steps', workflow_provenance=LTX_PROVENANCE)

        def save():
            temporary = folder / "report.tmp"
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(folder / "report.json")

        def event(stage, **fields):
            manifest["events"].append({"stage": stage, "elapsed_seconds": round(time.perf_counter() - started, 3), **fields})
            save()
            record("video_progress", job_id=job_id, stage=stage, execution_mode=manifest['execution_mode'], **fields)

        endpoint = None
        try:
            async with asyncio.timeout(self.settings.job_timeout):
                event("validating")
                plans = [] if automatic else plan_clips(len(request.photos), duration,
                                                        smoke=smoke, story_mode=request.story_mode)
                photos = []
                invalid = []
                for index, source in enumerate(request.photos):
                    destination = folder / f"photo-{index:04d}.png"
                    try:
                        photo_source = source
                        converted_raw = (automatic and source.suffix.lower() == '.dng' and sys.platform == 'darwin'
                                         and shutil.which('sips') and source.is_file() and source.stat().st_size <= 100_000_000)
                        if converted_raw:
                            photo_source = folder / f'raw-{index:04d}.jpg'
                            await self.media.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '90',
                                                 '--resampleHeightWidthMax', '6000', str(source), '--out', str(photo_source)])
                        info = await asyncio.to_thread(prepare_photo, photo_source, destination,
                                                       self.settings.max_input_bytes, fit=request.photo_fit)
                    except VideoError as error:
                        if not automatic or error.reason in {'photo_write_failed', 'photo_fit_invalid'}:
                            raise
                        invalid.append({'source_index': index, 'decision': 'invalid_input', 'reason': error.reason})
                        event('photo_skipped', photo_index=index, reason=error.reason)
                        continue
                    if automatic:
                        info.update(source_index=index, source_file=str(source))
                        if converted_raw:
                            info['source_conversion'] = 'macOS_sips_DNG_to_JPEG_quality90_maxedge6000'
                    manifest.setdefault('preprocessing', []).append(info)
                    warnings = info['quality']['warnings']
                    if warnings:
                        event('photo_quality_warning', photo_index=index, warnings=warnings)
                    if not automatic and request.quality_policy == 'REJECT' and set(warnings) & {'BLURRY', 'UNDEREXPOSED', 'OVEREXPOSED'}:
                        raise VideoError('photo_quality_rejected', photo_index=index, warnings=warnings)
                    photos.append(destination)
                if automatic:
                    event('selecting_photos')
                    selection = await asyncio.to_thread(
                        select_photos, manifest.get('preprocessing', []),
                        max_photos=min(24,max(12,request.max_selected_photos*4)) if single_ltx else request.max_selected_photos,
                        transition_seconds=request.transition_seconds,
                        max_duration_seconds=180 if single_ltx else request.max_duration_seconds)
                    selection['photos'] = sorted(selection['photos'] + invalid, key=lambda row: row['source_index'])
                    selection['input_count'] = len(request.photos)
                    selection['invalid_input_count'] = len(invalid)
                    if single_ltx:
                        candidate_indices=scene_candidates(selection)
                        event('planning_travel_story',candidate_count=len(candidate_indices))
                        storyboard=request.storyboard
                        story_meta={'source':'PROVIDED'}
                        if storyboard is None:
                            storyboard,story_meta=await plan_travel_story(self.http,self.settings,
                                [(i,folder/f'photo-{i:04d}.png') for i in candidate_indices],
                                max_photos=request.max_selected_photos,duration=request.duration_seconds,
                                context=request.memory_context)
                        selected_story=validate_storyboard(storyboard,candidate_indices,request.max_selected_photos)
                        request=request.model_copy(update={'storyboard':storyboard})
                        scene_pairs=all(shot.end_source_index is not None for shot in storyboard.shots)
                        scene_durations=story_durations(storyboard,request.duration_seconds)
                        if scene_durations and not scene_pairs:raise VideoError('timed_storyboard_requires_pairs')
                        manifest['travel_storyboard']={**story_meta,**storyboard.model_dump()}
                        selection['opencv_candidate_source_indices']=candidate_indices
                        selection['selected_source_indices']=selected_story
                        selection['selected_count']=len(selected_story)
                        selection['max_selected_photos']=request.max_selected_photos
                        for row in selection['photos']:
                            if row['source_index'] in selected_story:
                                row['decision']='selected'
                            elif row['decision']=='selected':
                                row['decision']='story_not_selected'
                        (folder/'storyboard.json').write_text(json.dumps(manifest['travel_storyboard'],ensure_ascii=False,indent=2)+'\n')
                    manifest['photo_selection'] = selection
                    manifest['selected_photo_count'] = selection['selected_count']
                    selected = selection['selected_source_indices']
                    photos = [folder / f'photo-{index:04d}.png' for index in selected]
                    if single_ltx:
                        selection.update(duration_seconds=request.duration_seconds,
                                         max_duration_seconds=request.max_duration_seconds,
                                         generation_request_count=1, reference_interval_count=len(photos)-1)
                        selection.pop('transition_count',None)
                        selection.pop('transition_seconds',None)
                    duration = selection['duration_seconds']
                    manifest['target_duration_seconds'] = duration
                    plans = [] if single_ltx else plan_clips(len(photos), duration, story_mode='TRANSITION')
                    manifest['planned_clips'] = [{**keyframe_plan(len(photos),duration,width=self.settings.ltx_width,height=self.settings.ltx_height,scene_pairs=scene_pairs,scene_durations=scene_durations),
                        'source_indices':selected}] if single_ltx else [
                        {**asdict(plan), 'start_source_index': selected[plan.start_index],
                         'end_source_index': selected[plan.end_index]} for plan in plans]
                    (folder/'selection.json').write_text(json.dumps(selection, ensure_ascii=False, indent=2)+'\n')
                    event('photos_selected', selected_count=len(photos), generation_request_count=1 if single_ltx else len(plans), duration_seconds=duration)
                    if request.preview_only:
                        if single_ltx:
                            graph = build_ltx_workflow(self.settings,images=[p.name for p in photos],duration=duration,
                                prompt=single_video_prompt(request),seed=request.seed,prefix=f'audigo/{job_id}/clip-0000',
                                reference_strength=request.reference_strength,scene_pairs=scene_pairs,scene_durations=scene_durations)
                            (folder/'planned-workflow-0000.json').write_text(json.dumps(graph,ensure_ascii=False,indent=2))
                        for index, plan in enumerate(plans):
                            graph = build_workflow(
                                self.settings, start=photos[plan.start_index].name, end=photos[plan.end_index].name,
                                frames=plan.frames, fps=plan.fps, prompt=memory_prompt(request, plan, index),
                                seed=(request.seed + index) % (2**63), prefix=f'audigo/{job_id}/clip-{index:04d}')
                            (folder/f'planned-workflow-{index:04d}.json').write_text(json.dumps(graph, ensure_ascii=False, indent=2))
                        manifest.update(status='selection_completed', execution_mode='selection_preview',
                                        gpu_inference_executed=False)
                        event('selection_completed')
                        return manifest
                event('preparing_music')
                audio, manifest['music'] = await self.media.prepare_music(
                    request.music, folder, duration, request.memory_style, request.memory_context)
                if single_ltx:
                    event('discovering')
                    endpoint = await self.resolver.resolve()
                    reserved_started = time.perf_counter()
                    manifest['pod_id'] = endpoint.pod_id
                    client = ComfyUI(self.http,endpoint,self.settings)
                    manifest['runtime'] = await client.runtime_info(require_cuda=self.execution_mode=='live')
                    info = await client.object_info()
                    def ltx_graph(images):
                        return build_ltx_workflow(self.settings,images=images,duration=duration,
                            prompt=single_video_prompt(request),seed=request.seed,prefix=f'audigo/{job_id}/clip-0000',
                            reference_strength=request.reference_strength,scene_pairs=scene_pairs,scene_durations=scene_durations)
                    validate_workflow(ltx_graph([p.name for p in photos]),info)
                    event('uploading')
                    uploaded = [await client.upload(path,f'audigo/{job_id}') for path in photos]
                    graph = ltx_graph(uploaded)
                    validate_workflow(graph,info)
                    (folder/'workflow-0000.json').write_text(json.dumps(graph,ensure_ascii=False,indent=2))
                    entry = {'index':0,**keyframe_plan(len(photos),duration,width=self.settings.ltx_width,height=self.settings.ltx_height,scene_pairs=scene_pairs,scene_durations=scene_durations),'source_indices':selected,
                             'status':'submitting','prompt_id':None}
                    manifest['clips'].append(entry)
                    event('generating',clip_index=0,clip_count=1)
                    manifest['gpu_inference_executed'] = None if self.execution_mode=='live' else False
                    prompt_started = time.perf_counter()
                    prompt_id = await client.submit(graph,job_id)
                    manifest['gpu_inference_executed'] = self.execution_mode=='live'
                    entry.update(prompt_id=prompt_id,status='submitted')
                    event('submitted',clip_index=0,prompt_id=prompt_id)
                    def ltx_state(state):
                        entry['status']=state
                        event('comfyui_'+state,clip_index=0,prompt_id=prompt_id)
                    result = await client.wait(prompt_id,'83',ltx_state)
                    entry.update(provider_execution_seconds=result['execution_seconds'],asset=result['asset'],status='downloading')
                    event('downloading',clip_index=0,prompt_id=prompt_id)
                    destination = folder/'clip-0000.mp4'
                    await client.download(result['asset'],destination)
                    native = await self.media.check_clip(destination,duration,size=(self.settings.ltx_width,self.settings.ltx_height))
                    entry.update(media=native,status='completed',request_to_clip_seconds=round(time.perf_counter()-prompt_started,3))
                    if request.music.file is None and request.music.preset!='NONE' and native['audio_streams']:
                        audio = folder/'generated-music.wav'
                        await self.media.run([self.settings.ffmpeg_bin,'-hide_banner','-loglevel','error','-nostdin','-y',
                            '-i',str(destination),'-map','0:a:0','-t',str(duration),'-ac','2','-ar','44100','-c:a','pcm_s16le',str(audio)])
                        manifest['music'].update(source='MODEL_GENERATED' if self.execution_mode=='live' else 'MOCK_GENERATED_AUDIO')
                    manifest['editing']={'applied':False,'policy':'keep_requested_duration_review_motion_instead_of_cutting_to_slides'}
                    manifest['generated_duration_seconds']=duration
                    manifest['visual_quality_status']='pending_revisit_motion_review'
                    event('encoding')
                    output,media = await self.media.finalize([destination],folder,duration,audio=audio,
                        volume=request.music.volume,memory_style=request.memory_style,fps=24,storyboard=request.storyboard)
                    manifest['memory_presentation']={'title':request.storyboard.title,'closing_line':request.storyboard.closing_line,
                        'emotional_arc':request.storyboard.emotional_arc,'scene_durations':scene_durations,
                        'music_direction':request.storyboard.music_direction}
                    manifest.update(output_file=str(output),media=media)
                    event('encoded')
                    return manifest
                if local_motion:
                    clips = []
                    for index, plan in enumerate(plans):
                        event('photo_motion_rendering', clip_index=index, clip_count=len(plans))
                        destination = folder / f'clip-{index:04d}.mp4'
                        media = await self.media.photo_motion(photos[plan.start_index], destination, plan.frames, index)
                        manifest['clips'].append({'index': index, **asdict(plan), 'status': 'completed', 'media': media})
                        clips.append(destination)
                    event('encoding')
                    output, media = await self.media.finalize(
                        clips, folder, duration, audio=audio,
                        volume=request.music.volume, memory_style=request.memory_style)
                    manifest.update(output_file=str(output), media=media)
                    event('encoded')
                    # finally가 완료 시간·성공 상태를 동일한 manifest에 기록한다.
                    return manifest
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
                    prompt=memory_prompt(request, first, 0), seed=request.seed, prefix=f"audigo/{job_id}/clip-0000",
                ), info)
                event("uploading")
                uploaded = [await client.upload(path, f"audigo/{job_id}") for path in photos]
                clips = []
                for index, plan in enumerate(plans):
                    entry = {"index": index, **asdict(plan), "status": "submitting", "prompt_id": None}
                    if automatic:
                        entry.update(start_source_index=selected[plan.start_index], end_source_index=selected[plan.end_index])
                    manifest["clips"].append(entry)
                    event("generating", clip_index=index, clip_count=len(plans))
                    workflow = build_workflow(
                        self.settings, start=uploaded[plan.start_index], end=uploaded[plan.end_index],
                        frames=plan.frames, fps=plan.fps, prompt=memory_prompt(request, plan, index),
                        seed=(request.seed + index) % (2**63), prefix=f"audigo/{job_id}/clip-{index:04d}",
                    )
                    validate_workflow(workflow, info)
                    (folder / f"workflow-{index:04d}.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=2))
                    prompt_started = time.perf_counter()
                    try:
                        manifest['gpu_inference_executed'] = None if self.execution_mode == 'live' else False
                        prompt_id = await client.submit(workflow, job_id)
                        manifest['gpu_inference_executed'] = self.execution_mode == 'live'
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
                output, media = await self.media.finalize(
                    clips, folder, duration, audio=audio,
                    volume=request.music.volume, memory_style=request.memory_style)
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
                manifest["status"] = "mock_completed" if self.execution_mode == "mock" and not local_motion else "completed"
                event("completed")
            manifest['latency_target_met'] = (
                manifest['status'] == 'completed' and ended - started <= manifest['latency_target_seconds']
                if manifest['latency_target_seconds'] is not None and (self.execution_mode == 'live' or local_motion) else None
            )
            save()
            record("video_finished", job_id=job_id, status=manifest["status"], execution_mode=manifest['execution_mode'],
                   total_seconds=manifest["total_seconds"], estimated_cost_usd=manifest["estimated_request_window_cost_usd"])
        return manifest
