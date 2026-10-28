"""Mock ComfyUI와 실제 FFmpeg를 이용해 영상 생성 흐름·오류 처리·측정 기록을 검사한다."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import httpx

from ai_service.schemas.video import ClipRequest, VideoRequest
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from scripts.video.mock_comfyui import MockComfyUI
from scripts.video.run import summarize

try:
    from PIL import Image
    from ai_service.services.video.pipeline import VideoPipeline
    VIDEO_EXTRAS = True
except ImportError:
    VIDEO_EXTRAS = False


@unittest.skipUnless(VIDEO_EXTRAS and shutil.which("ffmpeg") and shutil.which("ffprobe"), "video extras and FFmpeg required")
class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = VideoSettings(workspace=self.root / "jobs", comfyui_url="http://mock", poll_interval=0.001)
        self.photos = []
        for index, color in enumerate(("red", "blue", "green")):
            photo = self.root / f"photo-{index}.jpg"
            Image.new("RGB", (96, 64), color=color).save(photo)
            self.photos.append(photo)

    def tearDown(self):
        self.temporary.cleanup()

    async def run_job(self, request=None, *, failure=None, settings=None):
        mock = MockComfyUI(failure=failure)
        http = httpx.AsyncClient(transport=httpx.MockTransport(mock.handle))
        self.addAsyncCleanup(http.aclose)
        pipeline = VideoPipeline(http, settings or self.settings, execution_mode="mock")
        request = request or VideoRequest(photos=self.photos, duration_seconds=10)
        return await pipeline.generate(request), mock

    async def test_three_photos_two_generated_requests_and_real_ffmpeg_10s_mp4(self):
        result, mock = await self.run_job()
        self.assertEqual(result["status"], "mock_completed")
        self.assertEqual(len(mock.prompts), 2)
        self.assertEqual(len(mock.uploads), 3)
        self.assertEqual(result["media"]["duration_seconds"], 10)
        self.assertEqual(result["media"]["codec"], "h264")
        self.assertEqual(result["media"]["pixel_format"], "yuv420p")
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (1280, 720))
        self.assertLess(result["media"]["size_bytes"], 500_000_000)
        self.assertIsNone(result["estimated_request_window_cost_usd"])
        self.assertIsNone(result["gpu_billed_seconds"])
        self.assertIsNone(result["actual_cost_usd"])
        for clip in result["clips"]:
            self.assertEqual(clip["status"], "completed")
            self.assertTrue(clip["prompt_id"].startswith("mock-"))
            self.assertGreater(clip["request_to_clip_seconds"], 0)
        self.assertEqual([c["end_index"] for c in result["clips"]], [1, 2])
        saved = json.loads((Path(result["output_file"]).parent / "report.json").read_text())
        self.assertEqual(saved, result)
        summary = summarize(self.settings.workspace)
        self.assertEqual(summary["production_500_feasibility"], "not_validated")
        self.assertIsNone(summary["comparisons"][0]["estimated_cost_per_completed_video_including_failures"])

    async def test_two_photos_single_clip_smoke_and_arbitrary_final_duration(self):
        smoke, _ = await self.run_job(ClipRequest(photos=self.photos[:2]))
        self.assertEqual(smoke["test_kind"], "single_clip_smoke")
        self.assertEqual(len(smoke["clips"]), 1)
        final, mock = await self.run_job(VideoRequest(photos=self.photos[:2], duration_seconds=11))
        self.assertEqual(len(mock.prompts), 3)
        self.assertAlmostEqual(final["media"]["duration_seconds"], 11, places=2)
        self.assertEqual([c["kind"] for c in final["clips"]], ["transition", "motion_hold", "motion_hold"])

    async def test_errors_are_recorded_without_final_output_or_secret_text(self):
        for failure, reason in (("missing_model", "comfyui_value_unavailable"),
                                ("rejected", "comfyui_prompt_rejected"),
                                ("execution", "comfyui_execution_failed"),
                                ("missing_output", "comfyui_video_missing"),
                                ("download", "comfyui_download_failed"),
                                ("lost_submission", "comfyui_submission_uncertain")):
            with self.subTest(failure=failure), self.assertRaises(VideoError) as error:
                await self.run_job(failure=failure)
            self.assertEqual(error.exception.reason, reason)
        reports = [json.loads(p.read_text()) for p in self.settings.workspace.glob("*/report.json")]
        self.assertEqual(len(reports), 6)
        self.assertTrue(all(r["status"] == "failed" and r["total_seconds"] >= 0 for r in reports))
        self.assertFalse(list(self.settings.workspace.glob("*/final.mp4")))
        self.assertFalse(list(self.settings.workspace.glob("*/*.part")))

    async def test_timeout_and_uncertain_post_never_resubmit_or_interrupt(self):
        for failure in ("timeout", "lost_submission"):
            mock = MockComfyUI(failure=failure)
            async with httpx.AsyncClient(transport=httpx.MockTransport(mock.handle)) as http:
                pipeline = VideoPipeline(http, replace(self.settings, prompt_timeout=0.03), execution_mode="mock")
                with self.assertRaises((VideoError, TimeoutError)):
                    await pipeline.generate(ClipRequest(photos=self.photos[:2]))
            self.assertEqual(len(mock.prompts), 1)
            self.assertFalse(any(path == "/interrupt" for _, path, _ in mock.calls))
        for path in self.settings.workspace.glob("*/report.json"):
            self.assertTrue(json.loads(path.read_text())["gpu_work_may_still_be_running"])

    async def test_live_mode_requires_cuda_before_submitting_and_strips_private_stats(self):
        mock = MockComfyUI()
        def handle(request):
            if request.url.path == "/system_stats":
                return httpx.Response(200, json={"system": {"argv": ["private-token"]}, "devices": []})
            return mock.handle(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with self.assertRaises(VideoError) as error:
                await VideoPipeline(http, self.settings).generate(ClipRequest(photos=self.photos[:2]))
        self.assertEqual(error.exception.reason, "comfyui_gpu_unavailable")
        self.assertFalse(mock.prompts)
        for path in self.settings.workspace.glob("*/report.json"):
            self.assertNotIn("private-token", path.read_text())

    async def test_clip_download_cap_and_final_validation_remove_partial_files(self):
        with self.assertRaises(VideoError) as error:
            await self.run_job(ClipRequest(photos=self.photos[:2]), settings=replace(self.settings, max_clip_bytes=1))
        self.assertEqual(error.exception.reason, "comfyui_clip_too_large")
        with patch("ai_service.services.video.media.MAX_VIDEO_BYTES", 1), self.assertRaises(VideoError):
            await self.run_job(ClipRequest(photos=self.photos[:2]))
        self.assertFalse(list(self.settings.workspace.glob("*/*.partial.mp4")))
        self.assertFalse(list(self.settings.workspace.glob("*/*.part")))

    async def test_one_job_remains_on_original_pod_through_multiple_prompts(self):
        mock = MockComfyUI()
        current, lists = ["original"], []
        settings = replace(self.settings, endpoint_mode="runpod", runpod_api_key="secret")
        def handle(request):
            if request.url.host == "rest.runpod.io":
                lists.append(1)
                return httpx.Response(200, json=[{"id": current[0], "name": "audigo-video-test", "desiredStatus": "RUNNING", "ports": ["8188/http"]}])
            response = mock.handle(request)
            if request.url.path == "/prompt":
                current[0] = "replacement"
            return response
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            result = await VideoPipeline(http, settings, execution_mode="mock").generate(VideoRequest(photos=self.photos, duration_seconds=10))
        self.assertEqual(result["pod_id"], "original")
        self.assertEqual(len(lists), 1)
        self.assertTrue(all(host == "original-8188.proxy.runpod.net" for _, _, host in mock.calls))
