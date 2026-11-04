"""Opt-in ACE-Step 1.5 instrumental graph using native ComfyUI nodes.

Does not download, install, start a GPU or replace the selected user soundtrack.
Audition generated output before using it; model branding is not a quality test.
"""
from copy import deepcopy
from pathlib import PurePosixPath

from ai_service.clients.comfyui import asset_reference
from ai_service.services.video.errors import VideoError
from ai_service.services.video.workflow import validate_workflow


ACE_TEMPLATE_REVISION = 'c067929d7e9cc0f3fd15be348fa1a1d85da15946'
ACE_MODEL_REPOSITORY = 'Comfy-Org/ACE-Step-1.5'
ACE_MODEL_REVISION = '371bb1680f8c88d8c1083f6ffb6330d3d55bff76'
ACE_OUTPUT_NODE = '10'

ACE_MUSIC_PROVENANCE = {
    'model_repository': ACE_MODEL_REPOSITORY,
    'model_revision': ACE_MODEL_REVISION,
    'template_revision': ACE_TEMPLATE_REVISION,
    'template_sources': {
        'turbo': f'https://github.com/Comfy-Org/workflow_templates/blob/{ACE_TEMPLATE_REVISION}/templates/audio_ace_step_1_5_split.json',
        'xl_turbo': f'https://github.com/Comfy-Org/workflow_templates/blob/{ACE_TEMPLATE_REVISION}/templates/audio_ace_step1_5_xl_turbo.json',
    },
    'conversion': 'Official split graph: DualCLIP type ace, LM codes enabled, AuraFlow shift 3, Euler/simple 8 steps, CFG 1, audio VAE decode. SaveAudio writes lossless FLAC.',
    'license_source': 'https://huggingface.co/ACE-Step/Ace-Step1.5',
    'quality_status': 'Requires actual generation, listening and audio QA; not a commercial-quality guarantee.',
}

_FILES = {
    'turbo': ('diffusion_models', 'acestep_v1.5_turbo.safetensors', 4787825604, '3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7'),
    'xl_turbo': ('diffusion_models', 'acestep_v1.5_xl_turbo_bf16.safetensors', 9974719892, '86a1afb0a1f711f0e3304ff65d874df3ae6783db683dcf982513fb9b6d14ae71'),
    'caption': ('text_encoders', 'qwen_0.6b_ace15.safetensors', 1191588248, 'fd4590c82153b8ddb67e15a2e7aaa8afa8b83a858c8a9b82a4831063156aa7a7'),
    'lm_1_7b': ('text_encoders', 'qwen_1.7b_ace15.safetensors', 3708523360, 'ed63e9247d1f55f3ace04fa11e95b085fc82d459c82c5626f0b2e37b91ebd710'),
    'lm_4b': ('text_encoders', 'qwen_4b_ace15.safetensors', 8379154232, 'ffe5ffb855086c2ab55e467e9859fb01894781020a0376484dd19de166b79873'),
    'vae': ('vae', 'ace_1.5_vae.safetensors', 337431732, '6de92e3a862acd287e08b024ac90f0783a8635451b728721a33ff03565bcb2bb'),
}

JOYFUL_TRAVEL_CAPTION = (
    'Instrumental organic indie-pop for a joyful travel film, 120 BPM, D major, 4/4. '
    'Warm syncopated acoustic guitar, playful piano, melodic electric bass, human-played drums and shakers; '
    'airy polished stereo production, sunny, intimate and confident. '
    'A tender two-bar opening, clear groove by 4 seconds, a widening lift at 12 seconds, '
    'a joyful peak at 18-24 seconds, then a resolving final chord at 28 seconds with a two-second reverb tail.'
)


def ace_music_downloads(profile='turbo'):
    """Pinned public files with authoritative HF sizes and LFS SHA-256 hashes."""
    if profile not in ('turbo', 'xl_turbo'):
        raise VideoError('ace_music_profile_invalid')
    keys=[profile, 'caption', 'lm_4b' if profile=='xl_turbo' else 'lm_1_7b', 'vae']
    downloads=[]
    for key in keys:
        directory,name,size,digest=_FILES[key]
        relative=f'{directory}/{name}'
        downloads.append({'filename':name,'directory':directory,'relative_path':relative,
            'size_bytes':size,'sha256':digest,
            'url':f'https://huggingface.co/{ACE_MODEL_REPOSITORY}/resolve/{ACE_MODEL_REVISION}/split_files/{relative}'})
    return downloads


def build_ace_music_workflow(*, profile='turbo', duration=30, bpm=120, keyscale='D major',
                             seed=1201, prefix='audigo/music/joyful-travel', caption=JOYFUL_TRAVEL_CAPTION):
    """Build one complete instrumental composition, with a matching audio-code duration."""
    models=ace_music_downloads(profile)
    if (type(duration) not in (int,float) or not 10<=duration<=600
            or type(bpm) is not int or not 10<=bpm<=300
            or type(seed) is not int or not 0<=seed<=2**64-1
            or not isinstance(caption,str) or not caption.strip() or len(caption)>512):
        raise VideoError('ace_music_request_invalid')
    if (not isinstance(prefix,str) or not prefix or '\\' in prefix
            or PurePosixPath(prefix).is_absolute() or '..' in PurePosixPath(prefix).parts):
        raise VideoError('ace_music_prefix_invalid')
    graph={}
    def node(identifier, kind, **inputs):
        graph[identifier]={'class_type':kind,'inputs':inputs}
        return [identifier,0]
    model=node('1','UNETLoader',unet_name=models[0]['filename'],weight_dtype='default')
    clip=node('2','DualCLIPLoader',clip_name1=models[1]['filename'],clip_name2=models[2]['filename'],type='ace',device='default')
    vae=node('3','VAELoader',vae_name=models[3]['filename'])
    positive=node('4','TextEncodeAceStepAudio1.5',clip=clip,tags=caption.strip(),lyrics='[Instrumental]',
        seed=seed,bpm=bpm,duration=float(duration),timesignature='4',language='unknown',keyscale=keyscale,
        generate_audio_codes=True,cfg_scale=2.0,temperature=0.85,top_p=0.9,top_k=0,min_p=0.0)
    negative=node('5','ConditioningZeroOut',conditioning=positive)
    shifted=node('6','ModelSamplingAuraFlow',model=model,shift=3.0)
    latent=node('7','EmptyAceStep1.5LatentAudio',seconds=float(duration),batch_size=1)
    sampled=node('8','KSampler',model=shifted,seed=seed,steps=8,cfg=1.0,sampler_name='euler',scheduler='simple',
        positive=positive,negative=negative,latent_image=latent,denoise=1.0)
    audio=node('9','VAEDecodeAudio',samples=sampled,vae=vae)
    node(ACE_OUTPUT_NODE,'SaveAudio',audio=audio,filename_prefix=prefix)
    return graph


def validate_ace_music_workflow(graph, object_info):
    """Fail before submission if native nodes or downloaded weights are unavailable."""
    validate_workflow(graph,object_info)
    if object_info.get('SaveAudio',{}).get('output_node') is not True:
        raise VideoError('ace_music_output_node_invalid')


def ace_music_asset_from_history(history, prompt_id, output_node=ACE_OUTPUT_NODE):
    """Return a validated lossless asset when ready, or None while the job is pending.

    Native SaveAudio returns `audio`, not SaveVideo's `images`; the video wait()
    method is intentionally not reused. Download via the existing /view method.
    """
    record=history.get(prompt_id)
    if record is None:return None
    status=record.get('status',{})
    messages=status.get('messages',[])
    if status.get('status_str')=='error' or any(m[0] in ('execution_error','execution_interrupted') for m in messages if isinstance(m,list) and m):
        raise VideoError('ace_music_generation_failed')
    if status.get('completed') is not True:return None
    assets=record.get('outputs',{}).get(output_node,{}).get('audio',[])
    if len(assets)!=1 or not isinstance(assets[0],dict) or not str(assets[0].get('filename','')).lower().endswith('.flac'):
        raise VideoError('ace_music_asset_missing')
    return asset_reference(assets[0],expected_type='output')


def ace_music_provenance(profile='turbo'):
    result=deepcopy(ACE_MUSIC_PROVENANCE)
    result.update(profile=profile,downloads=ace_music_downloads(profile),output_node=ACE_OUTPUT_NODE)
    return result
