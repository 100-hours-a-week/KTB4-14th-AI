"""Input-independent, non-repeating memory-film executor with automatic gates."""
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
            score,score_report=await prepare_production_score(media,client,request.music,folder,duration_seconds=request.duration_seconds,style=request.memory_style,context=request.memory_context,seed=request.seed,object_info=info)
            report['music']=score_report;save('generating_scenes')
            takes=[];used_scenes=set();used_sources=set();reserves=list(plan.reserves);extra_attempts=sum(e["status"]=="rejected" for e in report["clips"])
            existing=[e for e in report["clips"] if e["status"]=="accepted"]
            already_used={e['scene_key'] for e in existing}
            reserves=[r for r in reserves if r.scene_key not in already_used]
            for number,initial in enumerate(plan.shots):
                if number<len(existing):
                    entry=existing[number];path=folder/f'scene-{number:02d}-attempt-{entry["attempt"]:02d}'/'native.mp4'
                    if not path.is_file():raise VideoError('memory_film_resume_asset_missing')
                    takes.append(FilmTake(path,entry['start_frame'],entry['frame_count'],entry['scene_key']))
                    used_scenes.add(entry['scene_key']);used_sources.update(entry['source_indices']);continue
                shot=initial;local_attempt=max([e.get('attempt',0)+1 for e in report['clips'] if e.get('scene_key')==initial.scene_key]+[0])
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
                    entry={'scene_key':shot.scene_key,'source_indices':shot.source_indices,'status':'submitting','attempt':local_attempt}
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
                    if not review.get('retryable_generation') or extra_attempts>=4:raise VideoError('memory_film_quality_gate_failed',scene=number)
                    extra_attempts+=1;local_attempt+=1
                    if local_attempt>1:
                        if not reserves:raise VideoError('memory_film_no_usable_reserve',scene=number)
                        shot=reserves.pop(0).with_frames(initial.frame_count,24)
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
        raise

async def finalize_accepted_scenes(http, settings, request, folder):
    """Graceful scene-count reduction, with every resized take checked again."""
    from ai_service.services.video.diversity import MemoryFilmPlan,allocate_scene_frames
    from ai_service.services.video.quality import review_people_fidelity
    folder=Path(folder);report=json.loads((folder/'report.json').read_text())
    plan=MemoryFilmPlan.model_validate_json((folder/'plan.json').read_text())
    approved=[(i,e) for i,e in enumerate(e for e in report['clips'] if e['status']=='accepted')]
    minimum=min(math.ceil(request.duration_seconds/4.5),len(plan.shots))
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
        score,music=await prepare_production_score(media,None,request.music,folder,duration_seconds=request.duration_seconds,style=request.memory_style,context=request.memory_context,seed=request.seed)
        result=await render_premium_film(media,takes,folder/('verified-film-'+str(round_number)),score,target_seconds=request.duration_seconds,music_volume=request.music.volume,opening_title=plan.title,closing_title=plan.closing_line)
        report.update(status='completed',output_file=result['output_file'],media=result['media'],scene_count=len(takes),scene_repeat_count=0,source_repeat_count=0,editing=result,music=music,quality_fallback='reallocated_and_rechecked_unique_scenes_with_independent_people_fidelity',excluded_source_slots=exclusions,final_source_indices=sorted(used))
        report['previous_failure_reason']=report.pop('failure_reason',None)
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return report
    raise VideoError('memory_film_fallback_review_failed')
