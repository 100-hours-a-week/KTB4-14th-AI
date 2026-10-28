"""영상 길이별 클립 계획과 공식 FLF2V 그래프의 연결·입력 검증을 검사한다."""
import hashlib
import json
import unittest

from pydantic import ValidationError

from ai_service.schemas.video import ClipRequest, VideoRequest
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.planning import plan_clips
from ai_service.services.video.workflow import WORKFLOWS, build_workflow, validate_workflow
from scripts.video.mock_comfyui import object_info


class PlanningTests(unittest.TestCase):
    def test_final_request_limits_and_smoke_are_distinct(self):
        for duration in (10, 25, 30, 60, 179, 180):
            self.assertEqual(VideoRequest(photos=["a", "b"], duration_seconds=duration).duration_seconds, duration)
        for duration in (5, 9, 181, 30.5, True):
            with self.assertRaises(ValidationError):
                VideoRequest(photos=["a", "b"], duration_seconds=duration)
        self.assertEqual(ClipRequest(photos=["a", "b"]).duration_seconds, 5)

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
