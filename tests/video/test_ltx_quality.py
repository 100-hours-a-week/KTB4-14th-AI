"""Quality path contracts: actual resolution, deterministic sampling, guide prep and preflight."""
import copy
import unittest

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.ltx_quality import build_ltx_quality_workflow
from ai_service.services.video.workflow import validate_workflow
from scripts.video.mock_comfyui import object_info


def quality_node_schema():
    # Independent required inputs recorded from ComfyUI 0.35.0's live object_info.
    info = object_info()
    info['SamplerEulerAncestral'] = {'input': {'required': {
        'eta': ['FLOAT', {'min': 0, 'max': 100}],
        's_noise': ['FLOAT', {'min': 0, 'max': 100}],
    }}}
    info['ImageScale'] = {'input': {'required': {
        'image': ['IMAGE'], 'upscale_method': [['nearest-exact', 'bilinear', 'area', 'bicubic', 'lanczos']],
        'width': ['INT', {'min': 0, 'max': 16384}], 'height': ['INT', {'min': 0, 'max': 16384}],
        'crop': [['disabled', 'center']],
    }}}
    info['LTXVPreprocess'] = {'input': {'required': {
        'image': ['IMAGE'], 'img_compression': ['INT', {'min': 0, 'max': 100}],
    }}}
    return info


def graph(**overrides):
    args = dict(images=['opening.png', 'closing.png'], duration=30,
                prompt='A continuous camera move through a real coastal place.', seed=123, prefix='quality/test')
    args.update(overrides)
    return build_ltx_quality_workflow(VideoSettings(), **args)


class LTXQualityTests(unittest.TestCase):
    def test_true_native_size_one_thirty_second_sampler_and_deterministic_noise(self):
        g = graph()
        validate_workflow(g, quality_node_schema())
        self.assertEqual(g['6']['inputs'], dict(width=1024, height=576, length=721, batch_size=1))
        self.assertEqual(g['15'], {'class_type': 'SamplerEulerAncestral', 'inputs': {'eta': 0.0, 's_noise': 1.0}})
        self.assertEqual(sum(node['class_type'] == 'SamplerCustomAdvanced' for node in g.values()), 1)
        self.assertEqual(sum(node['class_type'] == 'SaveVideo' for node in g.values()), 1)
        self.assertEqual(g['83']['class_type'], 'SaveVideo')
        self.assertEqual([g['200']['inputs']['frame_idx'], g['201']['inputs']['frame_idx']], [0, 720])
        self.assertEqual(g['20']['inputs']['samples'], ['19', 2])

    def test_both_guides_are_resized_then_compressed_before_conditioning(self):
        g = graph(native_width=1152, native_height=640)
        for i in range(2):
            guide = g[str(200+i)]['inputs']['image']
            preprocess = g[guide[0]]
            self.assertEqual(preprocess['class_type'], 'LTXVPreprocess')
            self.assertEqual(preprocess['inputs']['img_compression'], 25)
            resized = g[preprocess['inputs']['image'][0]]
            self.assertEqual(resized['inputs']['image'], [str(100+i), 0])
            self.assertEqual((resized['inputs']['width'], resized['inputs']['height']), (1152, 640))
        self.assertEqual((g['6']['inputs']['width'], g['6']['inputs']['height']), (1152, 640))

    def test_decode_configuration_is_explicit_and_settings_are_not_mutated(self):
        settings = VideoSettings(ltx_width=768, ltx_height=448)
        g = build_ltx_quality_workflow(settings, images=['a', 'b'], duration=30, prompt='move',
            seed=1, prefix='test', decode_temporal_size=4096, decode_temporal_overlap=64)
        self.assertEqual((settings.ltx_width, settings.ltx_height), (768, 448))
        self.assertEqual(g['20']['inputs']['temporal_size'], 4096)
        self.assertEqual(g['20']['inputs']['temporal_overlap'], 64)
        with self.assertRaises(VideoError):
            missing = copy.deepcopy(quality_node_schema()); missing.pop('LTXVPreprocess')
            validate_workflow(g, missing)

    def test_rejects_scene_montage_and_invalid_decode_before_submission(self):
        for overrides in [dict(images=['a']), dict(images=['a','b','c']), dict(duration=31),
                          dict(reference_strength=0), dict(decode_temporal_size=64, decode_temporal_overlap=48),
                          dict(native_width=1000), dict(reference_compression=101)]:
            with self.assertRaises(ValueError):graph(**overrides)
