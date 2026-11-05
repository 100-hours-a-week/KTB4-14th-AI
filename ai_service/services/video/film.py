"""사진별 장면을 생성하고 품질 검사를 거쳐 최종 여행 영상을 구성한다."""
import asyncio,json,math,sys,time,hashlib
from pathlib import Path
from ai_service.clients.comfyui import ComfyUI
from ai_service.clients.runpod import EndpointResolver
from ai_service.services.video.images import prepare_photo
from ai_service.services.video.storyboard import plan_memory_film,scene_candidates
from ai_service.services.video.selection import quality_score
from ai_service.services.video.quality import select_reviewed_take
from ai_service.services.video.score import prepare_production_score
from ai_service.services.video.editing import FilmTake
from ai_service.services.video.ltx_quality import build_ltx_quality_workflow
from ai_service.services.video.workflow import validate_workflow
from ai_service.services.video.media import Media
from ai_service.services.video.premium_render import render_premium_film
from ai_service.services.video.errors import VideoError

def recovery_shot(shot, reserves, frame_count):
    """Try a reserve, or remove a rejected cross-photo guide without inventing a new scene."""
    if reserves:
        return reserves.pop(0).with_frames(frame_count,24)
    if shot.source_index != shot.end_source_index:
        return shot.model_copy(update={'end_source_index':shot.source_index,
            'camera_path':'Keep the camera locked in the reference framing. No pan, zoom or newly revealed areas. Animate only the evidenced subjects and natural environmental motion.'})
    raise VideoError('memory_film_no_usable_reserve')

def fallback_minimum(duration, planned_count):
    # Small albums already allocate up to six seconds per distinct memory.
    maximum=6 if planned_count<=6 else 4.5
    return min(math.ceil(duration/maximum),planned_count)

def unused_photo_reserves(plan, clips, frames):
    """Small-album recovery: at most two untried photos, still subject to every gate."""
    from ai_service.services.video.diversity import MemoryFilmShot,usable_photo
    tried={i for c in clips if c['status']=='accepted' or len(c['source_indices'])==1 for i in c['source_indices']}
    photos=sorted((p for p in plan.catalog.photos if p.source_index not in tried and usable_photo(p)),
                  key=lambda p:(p.motion_potential,p.visual_quality),reverse=True)[:2]
    return [MemoryFilmShot(scene_key=f'recovery-{p.source_index}-{p.scene_key}',
        source_index=p.source_index,end_source_index=p.source_index,role=p.category,
        people_visible=p.people_visible,visible_scene=p.visible_scene,subject_action=p.safe_action,
        camera_path='Keep the original framing with a locked camera. No new foreground people or unseen faces. Show coherent natural motion in the evidenced scene.',
        memory_reason=p.memory_reason,quality_score=p.visual_quality,motion_potential=p.motion_potential,
        frame_count=frames,duration_seconds=frames/24) for p in photos]

async def generate_diverse_film(http,settings,request,folder,*,resolver=None,catalog=None,resume=False):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=resume)
    start=time.monotonic();media=Media(settings)
    report={'status':'running','backend':'ltx23_film','execution_mode':'live','test_kind':'final_video','target_duration_seconds':request.duration_seconds,'input_count':len(request.photos),'events':[],'clips':[]}
    signature=hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    if resume:
        previous=json.loads((folder/'report.json').read_text())
        if previous.get('request_signature')!=signature:raise VideoError('memory_film_resume_request_mismatch')
        if any(e['status'] in {'submitting','generating'} for e in previous['clips']) and previous.get('failure_reason') not in {'comfyui_download_failed','comfyui_execution_failed'}:
            raise VideoError('memory_film_provider_reconciliation_required')
        if previous['status']=='completed' and Path(previous['output_file']).is_file():return previous
        report=previous
        report.setdefault('resumed_failures',[]).append(report.pop('failure_reason',None))
        report['status']='running'
    report['request_signature']=signature
    elapsed_base=report.get("total_seconds",0) if resume else 0
    def save(stage=None):
        report['total_seconds']=round(elapsed_base+time.monotonic()-start,3)
        if stage:report['events'].append({'stage':stage,'elapsed_seconds':report['total_seconds']})
        p=folder/'report.tmp';p.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');p.replace(folder/'report.json')
    try:
        async with asyncio.timeout(settings.job_timeout):
            if resume:
                rows=report['photo_screening'];prepared={r['source_index']:folder/f"photo-{r['source_index']:04d}.png" for r in rows if r['decision']!='invalid_input'}
            else:
                prepared={};rows=[]
                for i,source in enumerate(request.photos):
                    path=folder/f'photo-{i:04d}.png'
                    try:
                        source=Path(source)
                        if source.suffix.lower()=='.dng' and sys.platform=='darwin':
                            converted=folder/f'raw-{i}.jpg'
                            await media.run(['sips','-s','format','jpeg','--resampleHeightWidthMax','6000',str(source),'--out',str(converted)])
                            source=converted
                        info=await asyncio.to_thread(prepare_photo,source,path,settings.max_input_bytes,fit=request.photo_fit)
                        warnings=info['quality']['warnings']
                        decision='quality_rejected' if set(warnings)&{'BLURRY','UNDEREXPOSED','OVEREXPOSED','LOW_RESOLUTION'} else 'selected'
                        rows.append({'source_index':i,'decision':decision,'quality_score':quality_score(info),'quality':info['quality']})
                        prepared[i]=path
                    except VideoError as e:rows.append({'source_index':i,'decision':'invalid_input','reason':e.reason})
                candidates=scene_candidates({'photos':rows,'selected_source_indices':[r['source_index'] for r in rows if r['decision']=='selected']})
            report['photo_screening']=rows;save('planning')
            if resume:
                from ai_service.services.video.diversity import MemoryFilmPlan
                plan=MemoryFilmPlan.model_validate_json((folder/'plan.json').read_text());meta=report['planning']
            else:
                plan,meta=await plan_memory_film(http,settings,[(i,prepared[i]) for i in candidates],duration=request.duration_seconds,context=request.memory_context,catalog=catalog)
            (folder/'plan.json').write_text(plan.model_dump_json(indent=2))
            report['planning']=meta;report['planned_scene_count']=len(plan.shots);save('planned')
            if request.preview_only:
                report['status']='selection_completed';save();return report
            endpoint=await (resolver or EndpointResolver(http,settings)).resolve()
            client=ComfyUI(http,endpoint,settings);info=await client.object_info()
            report['pod_id']=endpoint.pod_id;report['runtime']=await client.runtime_info(require_cuda=True)
            # Validate all video nodes and weights before the first paid submission.
            for shot in plan.shots:
                validate_workflow(build_ltx_quality_workflow(settings,images=['first.png','last.png'],duration=min(30,max(12,math.ceil(shot.duration_seconds+3))),prompt='Preflight coherent travel scene.',seed=request.seed,prefix='preflight'),info)
            score, score_report = await prepare_production_score(
                media, client, request.music, folder, duration_seconds=request.duration_seconds,
                style=request.memory_style, context=request.memory_context, seed=request.seed,
                object_info=info, allow_preset_fallback=settings.music_preset_fallback,
            )
            report['music']=score_report;save('generating_scenes')
            takes=[];used_scenes=set();used_sources=set();reserves=list(plan.reserves);extra_attempts=sum(e["status"]=="rejected" for e in report["clips"])
            existing=[e for e in report["clips"] if e["status"]=="accepted"]
            existing_by_slot={e.get('slot',i):e for i,e in enumerate(existing)}
            already_used={e['scene_key'] for e in existing}
            reserves=[r for r in reserves if r.scene_key not in already_used]
            pending=[*plan.shots,*[s.with_frames(round(request.duration_seconds/len(plan.shots)*24),24)
                     for s in plan.reserves if s.scene_key in report.get('recovery_scene_keys',[])]]
            for number,initial in enumerate(pending):
                if number in existing_by_slot:
                    entry=existing_by_slot[number];path=folder/f'scene-{number:02d}-attempt-{entry["attempt"]:02d}'/'native.mp4'
                    if not path.is_file():raise VideoError('memory_film_resume_asset_missing')
                    takes.append(FilmTake(path,entry['start_frame'],entry['frame_count'],entry['scene_key']))
                    used_scenes.add(entry['scene_key']);used_sources.update(entry['source_indices']);continue
                shot=initial;local_attempt=max([e.get('attempt',0)+1 for e in report['clips'] if e.get('scene_key')==initial.scene_key]+[0])
                if number in report.get('skipped_slots',[]) or local_attempt>=3:
                    report.setdefault('skipped_slots',[])
                    if number not in report['skipped_slots']:report['skipped_slots'].append(number)
                    save('scene_skipped');continue
                if local_attempt>1:
                    shot=recovery_shot(shot,reserves,initial.frame_count)
                while True:
                    if shot.scene_key in used_scenes or used_sources.intersection(shot.source_indices):raise VideoError('memory_film_repeated_scene')
                    attempt_folder=folder/f'scene-{number:02d}-attempt-{local_attempt:02d}';attempt_folder.mkdir(exist_ok=False)
                    refs=[prepared[shot.source_index],prepared[shot.end_source_index]]
                    images=[await client.upload(p,f'audigo/{folder.name}/{number}/{local_attempt}') for p in refs]
                    seconds=min(30,max(12,math.ceil(shot.duration_seconds+3)))
                    prompt=('A continuous cinematic live-action travel shot. The action visibly unfolds throughout the shot with real physical movement and clear depth parallax. '+shot.visible_scene+' '+shot.subject_action+' '+shot.camera_path+' Preserve the existing subjects and place. Natural location ambience.')
                    graph=build_ltx_quality_workflow(settings,images=images,duration=seconds,prompt=prompt,reference_strength=0.55 if shot.source_index==shot.end_source_index else 0.7,seed=(request.seed+number*31+local_attempt)%(2**63),prefix=f'audigo/{folder.name}/scene-{number}-{local_attempt}')
                    if shot.source_index == shot.end_source_index:
                        graph['201']['inputs']['strength'] = 0.15
                    validate_workflow(graph,info);(attempt_folder/'workflow.json').write_text(json.dumps(graph))
                    entry={'scene_key':shot.scene_key,'source_indices':shot.source_indices,'status':'submitting','attempt':local_attempt,'slot':number}
                    report['clips'].append(entry);save()
                    pid=await client.submit(graph,f'{folder.name}-{number}-{local_attempt}');entry.update(prompt_id=pid,status='generating');save()
                    result=await client.wait(pid,'83');output=attempt_folder/'native.mp4';
                    for download_attempt in range(3):
                        try:
                            await client.download(result['asset'],output);break
                        except VideoError as download_error:
                            if download_error.reason!='comfyui_download_failed' or download_attempt==2:raise
                            await asyncio.sleep(2*(download_attempt+1))
                    entry.update(provider_execution_seconds=result['execution_seconds'],status='checking');save()
                    review=await select_reviewed_take(http,settings,output,list(dict.fromkeys(refs)),duration_seconds=shot.duration_seconds,fps=24,visible_scene=shot.visible_scene,subject_action=shot.subject_action)
                    if review['accepted']:
                        from ai_service.services.video.quality import review_people_fidelity
                        fidelity=await review_people_fidelity(http,settings,output,list(dict.fromkeys(refs)),start_frame=review['start_frame'],frame_count=shot.frame_count)
                        review['people_fidelity']=fidelity
                        if not fidelity['accepted']:
                            review.update(accepted=False,reasons=['people_fidelity_rejected'],retryable_generation=not fidelity.get('unavailable',False))
                    (attempt_folder/'quality.json').write_text(json.dumps(review,ensure_ascii=False,indent=2))
                    if review['accepted']:
                        takes.append(FilmTake(output,review['start_frame'],shot.frame_count,shot.scene_key));used_scenes.add(shot.scene_key);used_sources.update(shot.source_indices)
                        entry.update(status='accepted',start_frame=review['start_frame'],frame_count=shot.frame_count);save('scene_accepted');break
                    entry.update(status='rejected',reasons=review.get('reasons'));save('scene_rejected')
                    if not review.get('retryable_generation'):raise VideoError('memory_film_quality_gate_failed',scene=number)
                    if extra_attempts>=4:
                        report.setdefault('skipped_slots',[]).append(number);save('scene_skipped');break
                    extra_attempts+=1;local_attempt+=1
                    if local_attempt>1:
                        try:shot=recovery_shot(shot,reserves,initial.frame_count)
                        except VideoError:
                            report.setdefault('skipped_slots',[]).append(number);save('scene_skipped');break
            if report.get('skipped_slots'):raise VideoError('memory_film_quality_gate_failed')
            if len(used_scenes)!=len(takes) or len({t.source for t in takes})!=len(takes):raise VideoError('memory_film_repeated_scene')
            result=await render_premium_film(media,takes,folder/'film',score,target_seconds=request.duration_seconds,music_volume=request.music.volume,opening_title=plan.title,closing_title=plan.closing_line,opening_window=(.3,min(2.4,request.duration_seconds-.1)))
            report.update(status='completed',output_file=result['output_file'],media=result['media'],scene_count=len(takes),scene_repeat_count=0,source_repeat_count=0,quality_policy='technical_and_reference_grounded_model_gate',visual_quality_status='automatically_screened_not_certified',editing=result)
            save('completed');return report
    except BaseException as e:
        report.update(status='failed',failure_reason=getattr(e,'reason',type(e).__name__));save('failed')
        if isinstance(e,VideoError) and e.reason in {'memory_film_quality_gate_failed','memory_film_no_usable_reserve','comfyui_download_failed'}:
            try:
                return await finalize_accepted_scenes(http,settings,request,folder)
            except VideoError as fallback_error:
                report['fallback_failure_reason']=fallback_error.reason;save('fallback_failed')
                if (fallback_error.reason=='memory_film_insufficient_approved_scenes'
                        and not report.get('recovery_pass_started') and len(plan.shots)<=6
                        and len(request.photos)<=10):
                    extra=unused_photo_reserves(plan,report['clips'],round(request.duration_seconds/len(plan.shots)*24))
                    if extra:
                        plan.reserves.extend(extra)
                        (folder/'plan.json').write_text(plan.model_dump_json(indent=2))
                        report.update(recovery_pass_started=True,recovery_scene_keys=[s.scene_key for s in extra])
                        save('recovering_unused_photos')
                        return await generate_diverse_film(http,settings,request,folder,resolver=resolver,resume=True)
        raise

async def finalize_accepted_scenes(http, settings, request, folder):
    """Graceful scene-count reduction, with every resized take checked again."""
    from ai_service.services.video.diversity import MemoryFilmPlan,allocate_scene_frames
    from ai_service.services.video.quality import review_people_fidelity
    folder=Path(folder);report=json.loads((folder/'report.json').read_text())
    plan=MemoryFilmPlan.model_validate_json((folder/'plan.json').read_text())
    approved=[(e.get('slot',i),e) for i,e in enumerate(e for e in report['clips'] if e['status']=='accepted')]
    minimum=fallback_minimum(request.duration_seconds,len(plan.shots))
    lookup={s.scene_key:s for s in [*plan.shots,*plan.reserves]}
    exclusions=[]
    for round_number in range(3):
        if len(approved)<minimum:raise VideoError('memory_film_insufficient_approved_scenes')
        scenes=[lookup[e['scene_key']] for _,e in approved]
        counts=allocate_scene_frames(scenes,request.duration_seconds,24)
        maximum=max(4.5,request.duration_seconds/minimum)
        if any(n/24>maximum for n in counts):
            total=round(request.duration_seconds*24);counts=[total//len(approved)+(i<total%len(approved)) for i in range(len(approved))]
        takes=[];used=set();keys=set();reviews=[];rejected=[]
        for (slot,entry),scene,count in zip(approved,scenes,counts):
            if scene.scene_key in keys or used.intersection(entry['source_indices']):raise VideoError('memory_film_repeated_scene')
            source=folder/f'scene-{slot:02d}-attempt-{entry["attempt"]:02d}'/'native.mp4'
            refs=[folder/f'photo-{i:04d}.png' for i in entry['source_indices']]
            anchor=entry['start_frame']
            review=await select_reviewed_take(http,settings,source,refs,duration_seconds=count/24,visible_scene=scene.visible_scene,subject_action=scene.subject_action,excluded_ranges=((0,anchor),(anchor+count,10000000)))
            if not review['accepted']:
                if review.get('review_status')=='unavailable':raise VideoError('memory_film_review_unavailable')
                rejected.append(slot);reviews.append({'scene_key':scene.scene_key,'frame_count':count,'review':review});continue
            fidelity=await review_people_fidelity(http,settings,source,refs,start_frame=review['start_frame'],frame_count=count)
            reviews.append({'scene_key':scene.scene_key,'frame_count':count,'review':review,'people_fidelity':fidelity})
            (folder/f'final-fidelity-reviews-round-{round_number}.json').write_text(json.dumps(reviews,ensure_ascii=False,indent=2))
            (folder/'final-fidelity-reviews.json').write_text(json.dumps(reviews,ensure_ascii=False,indent=2))
            if not fidelity['accepted']:
                if fidelity.get('unavailable'):raise VideoError('memory_film_people_review_unavailable')
                rejected.append(slot);continue
            takes.append(FilmTake(source,review['start_frame'],count,scene.scene_key));keys.add(scene.scene_key);used.update(entry['source_indices'])
        if rejected:
            exclusions.extend(rejected);approved=[pair for pair in approved if pair[0] not in rejected];continue
        media=Media(settings)
        score, music = await prepare_production_score(
            media, None, request.music, folder, duration_seconds=request.duration_seconds,
            style=request.memory_style, context=request.memory_context, seed=request.seed,
            allow_preset_fallback=settings.music_preset_fallback,
        )
        result=await render_premium_film(media,takes,folder/('verified-film-'+str(round_number)),score,target_seconds=request.duration_seconds,music_volume=request.music.volume,opening_title=plan.title,closing_title=plan.closing_line)
        report.update(status='completed',output_file=result['output_file'],media=result['media'],scene_count=len(takes),scene_repeat_count=0,source_repeat_count=0,editing=result,music=music,quality_fallback='reallocated_and_rechecked_unique_scenes_with_independent_people_fidelity',excluded_source_slots=exclusions,final_source_indices=sorted(used))
        report['previous_failure_reason']=report.pop('failure_reason',None)
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return report
    raise VideoError('memory_film_fallback_review_failed')
