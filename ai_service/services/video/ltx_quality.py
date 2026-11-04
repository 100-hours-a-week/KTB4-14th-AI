"""Opt-in LTX-2.3 quality workflow for one coherent First–Last scene.

This raises the actual diffusion resolution rather than merely enlarging the MP4.
It does not promise that generated geometry, identities or motion will be correct.
"""
from dataclasses import replace

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.ltx import build_ltx_workflow


LTX_QUALITY_PROVENANCE = {
    'official_flf_template': 'https://github.com/Comfy-Org/workflow_templates/blob/14220b8bd3e86baa852959f17039613191e66656/templates/video_ltx2_3_flf2v.json',
    'comfy_core_revision': '40c4fcdf513a4523e39d54a9d391908af8df8171',
    'official_pipeline_revision': 'e98f6d927d83c92ef2be6aec76c8440b19306e39',
    'official_pipeline_source': 'https://github.com/Lightricks/LTX-2/blob/e98f6d927d83c92ef2be6aec76c8440b19306e39/packages/ltx-pipelines/src/ltx_pipelines/distilled.py',
    'sampler': 'SamplerEulerAncestral(eta=0.0, s_noise=1.0), exact deterministic choice from the pinned FLF template. No CFG++ ancestral noise injection.',
    'reference_preprocessing': 'Resize to the diffusion dimensions, then LTXVPreprocess(img_compression=25), matching the pinned FLF preprocessing setting.',
    'native_default': [1024, 576],
    'decode_default': {'tile_size': 768, 'overlap': 64, 'temporal_size': 256, 'temporal_overlap': 32},
    'decode_note': 'Bounded temporal tiles for 80GB validation. The pinned FLF template uses temporal_size=4096 and temporal_overlap=64; full temporal decode is optional and may require more VRAM.',
    'limitations': 'One-stage generation, not the official learned spatial-upscaler/refinement pipeline. Thirty seconds remains a self-hosted trial. Actual quality, runtime and VRAM must be measured.',
}


def build_ltx_quality_workflow(
    settings: VideoSettings,
    *,
    images: list[str],
    duration: int,
    prompt: str,
    seed: int,
    prefix: str,
    reference_strength: float = 0.7,
    native_width: int = 1024,
    native_height: int = 576,
    decode_temporal_size: int = 256,
    decode_temporal_overlap: int = 32,
    reference_compression: int = 25,
) -> dict:
    """Build one scene, one sampler and output node 83, using caller-selected native size.

    Multiple unrelated places are deliberately excluded from this opt-in path:
    their transitions must not be presented as a clean single-location shot.
    ``native_width``/``native_height`` describe actual diffusion dimensions, not
    a post-processing export size. The input settings object is not mutated.
    """
    if len(images) != 2:
        raise ValueError('Quality First–Last generation requires exactly two scene references')
    if type(duration) is not int or not 1 <= duration <= 30:
        raise ValueError('Quality trial duration must be an integer between 1 and 30 seconds')
    if not 0 < reference_strength <= 1:
        raise ValueError('Reference strength must be greater than zero and at most one')
    if (type(reference_compression) is not int or not 0 <= reference_compression <= 100):
        raise ValueError('Reference compression must be an integer between 0 and 100')
    if (type(decode_temporal_size) is not int or type(decode_temporal_overlap) is not int
            or not 8 <= decode_temporal_size <= 4096 or decode_temporal_size % 8
            or not 4 <= decode_temporal_overlap <= decode_temporal_size // 2
            or decode_temporal_overlap % 4):
        raise ValueError('Invalid temporal decode tile size or overlap')
    native = replace(settings, ltx_width=native_width, ltx_height=native_height)
    graph = build_ltx_workflow(
        native, images=images, duration=duration, prompt=prompt, seed=seed,
        prefix=prefix, reference_strength=reference_strength,
    )
    # The previous euler_ancestral_cfg_pp path injects eta=1 noise and forces an
    # unconditional evaluation even at CFG=1. This is not the pinned FLF sampler.
    graph['15'] = {'class_type': 'SamplerEulerAncestral',
                   'inputs': {'eta': 0.0, 's_noise': 1.0}}
    for i in range(2):
        resize_id, preprocess_id = str(1500 + i), str(1600 + i)
        graph[resize_id] = {'class_type': 'ImageScale', 'inputs': {
            'image': [str(100 + i), 0], 'upscale_method': 'lanczos',
            'width': native_width, 'height': native_height, 'crop': 'center',
        }}
        graph[preprocess_id] = {'class_type': 'LTXVPreprocess', 'inputs': {
            'image': [resize_id, 0], 'img_compression': reference_compression,
        }}
        graph[str(200 + i)]['inputs']['image'] = [preprocess_id, 0]
    graph['20']['inputs'].update(
        tile_size=768, overlap=64, temporal_size=decode_temporal_size,
        temporal_overlap=decode_temporal_overlap,
    )
    return graph
