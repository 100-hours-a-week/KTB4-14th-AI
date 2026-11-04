"""LTX-2.3 단일 AV 확산 요청에 첫·중간·끝 사진 가이드를 연결한다."""
import math

from ai_service.services.video.music import choose_preset
from ai_service.services.video.errors import VideoError


LTX_PROVENANCE = {
    'model': 'Lightricks/LTX-2.3-fp8',
    'model_revision': '91eda216f820e5ecb3d17dc9fe3b7cdaf56b587d',
    'text_encoder_repository': 'Comfy-Org/ltx-2',
    'text_encoder_revision': 'ccde4ba417d7900669fd56dd292a883cee11ff37',
    'source': 'https://github.com/Comfy-Org/workflow_templates/blob/14220b8bd3e86baa852959f17039613191e66656/templates/video_ltx2_3_flf2v.json',
    'conversion': 'Use distilled checkpoint directly, official eight-step ManualSigmas, CFG 1 and explicit SamplerEulerAncestral(eta=0.0, s_noise=1.0) from the pinned FLF template. Native LTXVAddGuide chain conditions first/middle/last images; crop appended guide latents after sampling, then tiled VAE decode. One sampler and one MP4 output. Resolution, guide preprocessing and tiled decode remain local settings rather than a claim of full template equivalence.',
    'sampler': {'node': 'SamplerEulerAncestral', 'eta': 0.0, 's_noise': 1.0},
    'native_size': [768, 448], 'output_size': [1280, 720], 'fps': 24, 'steps': 8,
    'reference_strength': 0.7,
    'duration_note': '30 seconds is a self-hosted trial (721 frames), not a claim about the hosted API supported-duration table. Requires actual visual/memory validation.',
}


def story_durations(storyboard, duration):
    values=[shot.duration_seconds for shot in storyboard.shots]
    if all(value is None for value in values):return None
    if any(value is None for value in values) or sum(values)!=duration:
        raise VideoError('invalid_travel_storyboard_duration')
    return values


def keyframe_plan(count, duration, *, width=768, height=448, scene_pairs=False, scene_durations=None):
    frames = 8 * math.ceil(duration * 24 / 8) + 1
    if scene_pairs:
        if count<2 or count%2:raise ValueError('Scene pairs require an even reference count')
        scenes=count//2
        if scene_durations is not None:
            if len(scene_durations)!=scenes or any(v<=0 for v in scene_durations) or sum(scene_durations)!=duration:
                raise VideoError('invalid_travel_storyboard_duration')
            elapsed=0;starts=[]
            for value in scene_durations:
                starts.append(round(elapsed*24));elapsed+=value
        else:starts=[round(i*(frames-1)/scenes) for i in range(scenes)]
        ends=[start-1 for start in starts[1:]]+[frames-1]
        guides=[index for pair in zip(starts,ends) for index in pair]
    else:
        guides=[round(i*(frames-1)/(count-1)) for i in range(count)]
    return {'kind': 'single_keyframe_video', 'duration_seconds': duration, 'frames': frames, 'fps': 24,
            'width': width, 'height': height,
            'scene_pairs':scene_pairs,'scene_durations':scene_durations,'keyframe_indices':guides}


def single_video_prompt(request):
    """Describe the desired footage directly; negative concepts in positive text leaked into renders."""
    from ai_service.schemas.video import DEFAULT_MOTION
    preset = choose_preset(request.music, request.memory_style, request.memory_context)
    music = {'BRIGHT': 'Bright positive instrumental travel music.',
             'WARM': 'Warm uplifting instrumental piano and acoustic music.',
             'CALM': 'Calm positive instrumental music.',
             'NONE': 'Natural location ambience.'}[preset]
    parts=[f'Live-action first-person travel footage in South Korea, {request.duration_seconds} seconds. '
           'The camera moves physically through the real environment with foreground depth parallax.']
    storyboard=request.storyboard
    if storyboard:
        paired=all(shot.end_source_index is not None for shot in storyboard.shots)
        count=len(storyboard.shots)
        if paired:
            durations=story_durations(storyboard,request.duration_seconds)
            bounds=[0]
            for value in durations or [request.duration_seconds/count]*count:bounds.append(bounds[-1]+value)
            parts.append('Each shot moves continuously between its opening and closing viewpoints. '
                         'Adjacent shots meet with an instant hard cut.')
        else:
            anchors=[i*request.duration_seconds/(count-1) for i in range(count)] if count>1 else [0]
            bounds=[0]+[(a+b)/2 for a,b in zip(anchors,anchors[1:])]+[request.duration_seconds]
        for i,shot in enumerate(storyboard.shots):
            parts.append(f'{"HARD CUT. " if i else ""}{bounds[i]:.1f}-{bounds[i+1]:.1f}s: '
                         f'{shot.visible_scene} {shot.subject_action} {shot.camera_path}')
        if any(shot.people_visible is True for shot in storyboard.shots):
            parts.append('The existing Korean travelers keep their original appearance, clothes and number, '
                         'remaining at the original distance in the scene. The camera operator stays off screen.')
        elif all(shot.people_visible is False for shot in storyboard.shots):
            parts.append('The view stays on the landscape throughout.')
    else:
        parts.append('Walk through the South Korean landscape while wind moves the vegetation and water flows naturally.')
    if request.prompt != DEFAULT_MOTION:parts.append(request.prompt)
    parts.append(storyboard.music_direction if storyboard and storyboard.music_direction else music+' Natural sounds of the location.')
    return ' '.join(parts)


def build_ltx_workflow(settings, *, images, duration, prompt, seed, prefix, reference_strength=0.7, scene_pairs=False, scene_durations=None):
    plan = keyframe_plan(len(images), duration,width=settings.ltx_width,height=settings.ltx_height,scene_pairs=scene_pairs,scene_durations=scene_durations)
    graph = {}
    def node(identifier, kind, **inputs):
        graph[identifier] = {'class_type': kind, 'inputs': inputs}
        return [identifier, 0]
    node('1', 'CheckpointLoaderSimple', ckpt_name=settings.ltx_checkpoint)
    clip = node('2', 'LTXAVTextEncoderLoader', text_encoder=settings.ltx_text_encoder,
                ckpt_name=settings.ltx_checkpoint, device='default')
    positive = node('3', 'CLIPTextEncode', clip=clip, text=prompt)
    negative = node('4', 'CLIPTextEncode', clip=clip, text='new people, foreign tourists, extra companions, unrelated scenes, distorted faces, double exposure, text overlays, subtitles, dialogue, singing')
    node('5', 'LTXVConditioning', positive=positive, negative=negative, frame_rate=24.)
    latent = node('6', 'EmptyLTXVLatentVideo', width=plan['width'], height=plan['height'], length=plan['frames'], batch_size=1)
    positive, negative = ['5', 0], ['5', 1]
    for index, (image, frame) in enumerate(zip(images, plan['keyframe_indices'])):
        reference = node(str(100+index), 'LoadImage', image=image)
        identifier = str(200+index)
        node(identifier, 'LTXVAddGuide', positive=positive, negative=negative,
             vae=['1', 2], latent=latent, image=reference, frame_idx=frame, strength=reference_strength)
        positive, negative, latent = [identifier, 0], [identifier, 1], [identifier, 2]
    audio_vae = node('10', 'LTXVAudioVAELoader', ckpt_name=settings.ltx_checkpoint)
    audio = node('11', 'LTXVEmptyLatentAudio', frames_number=plan['frames'], frame_rate=24., batch_size=1, audio_vae=audio_vae)
    joint = node('12', 'LTXVConcatAVLatent', video_latent=latent, audio_latent=audio)
    guider = node('13', 'CFGGuider', model=['1', 0], positive=positive, negative=negative, cfg=1.)
    noise = node('14', 'RandomNoise', noise_seed=seed)
    # Explicit eta=0 matches the pinned distilled FLF workflow. The previous
    # CFG++ ancestral selection injected fresh noise with its eta=1 default.
    sampler = node('15', 'SamplerEulerAncestral', eta=0.0, s_noise=1.0)
    sigmas = node('16', 'ManualSigmas', sigmas='1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0')
    samples = node('17', 'SamplerCustomAdvanced', noise=noise, guider=guider, sampler=sampler, sigmas=sigmas, latent_image=joint)
    node('18', 'LTXVSeparateAVLatent', av_latent=samples)
    node('19', 'LTXVCropGuides', positive=positive, negative=negative, latent=['18', 0])
    decoded = node('20', 'VAEDecodeTiled', samples=['19', 2], vae=['1', 2], tile_size=512, overlap=64, temporal_size=64, temporal_overlap=8)
    audio_out = node('21', 'LTXVAudioVAEDecode', samples=['18', 1], audio_vae=audio_vae)
    video = node('22', 'CreateVideo', images=decoded, audio=audio_out, fps=24.)
    node('83', 'SaveVideo', video=video, filename_prefix=prefix, format='mp4', **{'format.codec':'h264','format.codec.encoding':'auto'})
    return graph
