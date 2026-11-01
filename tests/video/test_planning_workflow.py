"""영상 길이별 클립 계획과 공식 FLF2V 그래프의 연결·입력 검증을 검사한다."""
import hashlib
import json
import unittest
from dataclasses import replace

from pydantic import ValidationError

from ai_service.schemas.video import ClipRequest, VideoRequest
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.planning import plan_clips
from ai_service.services.video.workflow import WORKFLOWS, build_workflow, model_availability, validate_workflow
from scripts.video.mock_comfyui import object_info


class PlanningTests(unittest.TestCase):
    def test_memory_montage_generates_independent_motion_for_each_photo(self):
        clips = plan_clips(2, 5, smoke=True, story_mode='MONTAGE')
        self.assertEqual([(c.start_index, c.end_index) for c in clips], [(0, 0), (1, 1)])
        self.assertTrue(all(c.kind == 'memory_shot' and c.frames == 41 for c in clips))
        self.assertEqual(sum(c.duration_seconds for c in clips), 5)
        for seconds in (10, 30, 180):
            clips = plan_clips(3, seconds, story_mode='MONTAGE')
            self.assertTrue(all(c.start_index == c.end_index and c.frames <= 81 for c in clips))
            self.assertAlmostEqual(sum(c.duration_seconds for c in clips), seconds)

    def test_final_request_limits_and_smoke_are_distinct(self):
        for duration in (10, 25, 30, 60, 179, 180):
            self.assertEqual(VideoRequest(photos=["a", "b"], duration_seconds=duration).duration_seconds, duration)
        for duration in (5, 9, 181, 30.5, True):
            with self.assertRaises(ValidationError):
                VideoRequest(photos=["a", "b"], duration_seconds=duration)
        self.assertEqual(ClipRequest(photos=["a", "b"]).duration_seconds, 5)

    def test_large_photo_motion_batch_preserves_ai_capacity_and_duration(self):
        request = VideoRequest(photos=['photo.jpg'] * 110, duration_seconds=180, story_mode='PHOTO_MOTION')
        clips = plan_clips(len(request.photos), request.duration_seconds, story_mode=request.story_mode)
        self.assertEqual(len(clips), 110)
        self.assertEqual(sum(p.frames for p in clips), 180 * 16)
        self.assertTrue(all(p.duration_seconds >= 1.5 for p in clips))
        for mode in ('TRANSITION', 'MONTAGE'):
            with self.assertRaises(ValidationError):
                VideoRequest(photos=['photo.jpg'] * 92, duration_seconds=180, story_mode=mode)
        with self.assertRaises(ValidationError):
            VideoRequest(photos=['photo.jpg'] * 121, duration_seconds=180, story_mode='PHOTO_MOTION')

    def test_all_supported_durations_use_short_generated_clips(self):
        for seconds in range(10, 181):
            for photos in (2, 3, min(20, seconds // 2 + 1)):
                clips = plan_clips(photos, seconds)
                self.assertAlmostEqual(sum(c.duration_seconds for c in clips), seconds)
                self.assertTrue(all(c.frames % 4 == 1 and c.frames <= 81 for c in clips))
                self.assertTrue(all(c.duration_seconds <= 5 for c in clips))
                transitions = [(c.start_index, c.end_index) for c in clips if c.kind == "transition"]
                self.assertEqual(transitions, [(i, i + 1) for i in range(photos-1)])
                for first, second in zip(clips, clips[1:]):
                    self.assertEqual(first.end_index, second.start_index)
        with self.assertRaises(VideoError):
            plan_clips(7, 10)

    def test_two_photo_ten_seconds_is_transition_then_new_motion_hold(self):
        self.assertEqual([(c.start_index, c.end_index) for c in plan_clips(2, 10)], [(0, 1), (1, 1)])
        self.assertEqual(len(plan_clips(2, 5, smoke=True)), 1)


class WorkflowTests(unittest.TestCase):
    def graph(self):
        return build_workflow(VideoSettings(), start="inputs/a.png", end="inputs/b.png", frames=81,
                              fps=16.2, prompt="travel motion", seed=42, prefix="audigo/test")

    def test_model_diagnostics_distinguish_missing_files_from_ready_gpu(self):
        info = object_info()
        self.assertTrue(all(m['available'] for m in model_availability(self.graph(), info)))
        for kind, field in [('CLIPLoader', 'clip_name'), ('UNETLoader', 'unet_name'), ('VAELoader', 'vae_name')]:
            info[kind]['input']['required'][field] = [[]]
        models = model_availability(self.graph(), info)
        self.assertEqual(len(models), 4)
        self.assertTrue(all(not m['available'] and m['available_count'] == 0 for m in models))

    def test_fast_profile_matches_official_four_step_lora_branch(self):
        settings = replace(VideoSettings(), generation_profile='fast')
        graph = build_workflow(settings, start='a.png', end='b.png', frames=81,
                               fps=16.2, prompt='travel', seed=42, prefix='audigo/fast')
        source = json.loads((WORKFLOWS / 'wan2_2_flf2v.ui.json').read_text())
        nodes = {str(n['id']): n for n in source['nodes']}
        for node in ('91', '92'):
            self.assertEqual(graph[node]['inputs']['lora_name'], nodes[node]['widgets_values'][0])
            self.assertEqual(graph[node]['inputs']['strength_model'], nodes[node]['widgets_values'][1])
        self.assertEqual(graph['73']['inputs'], {'model': ['91', 0], 'shift': 5.0})
        self.assertEqual(graph['74']['inputs'], {'model': ['92', 0], 'shift': 5.0})
        for node, official in (('84', '57'), ('87', '58')):
            self.assertEqual(graph[node]['inputs']['steps'], nodes[official]['widgets_values'][3])
            self.assertEqual(graph[node]['inputs']['cfg'], nodes[official]['widgets_values'][4])
        self.assertEqual(graph['84']['inputs']['end_at_step'], graph['87']['inputs']['start_at_step'])
        self.assertEqual(graph['84']['inputs']['end_at_step'], 2)
        self.assertEqual((graph['81']['inputs']['width'], graph['81']['inputs']['height']), (1280, 720))
        self.assertEqual(graph['81']['inputs']['length'], 81)
        validate_workflow(graph, object_info())
        info = object_info()
        info['LoraLoaderModelOnly']['input']['required']['lora_name'] = [[]]
        self.assertEqual(sum(not m['available'] for m in model_availability(graph, info)), 2)
        with self.assertRaises(VideoError):
            validate_workflow(graph, info)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            VideoSettings(generation_profile='unknown')

    def test_pinned_source_checksum_and_active_links_match_official_graph(self):
        provenance = json.loads((WORKFLOWS / "provenance.json").read_text())
        raw = (WORKFLOWS / "wan2_2_flf2v.ui.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance["sha256"])
        source = json.loads(raw)
        graph = self.graph()
        links = {link[0]: [str(link[1]), link[2]] for link in source["links"]}
        for node in source["nodes"]:
            if str(node["id"]) not in graph:
                continue
            self.assertEqual(node["mode"], 0)
            for field in node["inputs"]:
                if field.get("link") is not None:
                    self.assertEqual(graph[str(node["id"])]["inputs"][field["name"]], links[field["link"]])
        self.assertEqual(graph["81"]["class_type"], "WanFirstLastFrameToVideo")
        self.assertEqual(graph["81"]["inputs"]["start_image"], ["80", 0])
        self.assertEqual(graph["81"]["inputs"]["end_image"], ["89", 0])
        self.assertEqual(graph["87"]["inputs"]["latent_image"], ["84", 0])
        self.assertEqual(graph["84"]["inputs"]["end_at_step"], graph["87"]["inputs"]["start_at_step"])
        validate_workflow(graph, object_info())

    def test_preflight_rejects_missing_node_model_and_dynamic_codec(self):
        graph = self.graph()
        for case in ("node", "model", "codec", "field"):
            info = object_info()
            candidate = self.graph()
            if case == "node":
                del info["WanFirstLastFrameToVideo"]
            elif case == "model":
                info["UNETLoader"]["input"]["required"]["unet_name"] = [[]]
            elif case == "codec":
                candidate["83"]["inputs"]["format.codec"] = "invented"
            else:
                candidate["81"]["inputs"]["invented_field"] = 3
            with self.assertRaises(VideoError):
                validate_workflow(candidate, info)
        self.assertEqual(graph["83"]["inputs"]["format.codec"], "h264")
