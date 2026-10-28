"""GPU 없이 ComfyUI API 연동을 검사하도록 합성 응답과 FFmpeg 테스트 영상을 제공한다."""
import json
from pathlib import Path
import subprocess
import tempfile

import httpx


def object_info():
    """API 그래프와 별도로 정의한 ComfyUI 0.35.0의 테스트 대상 노드 입력 형식을 제공한다."""
    def node(required, optional=None, output=False):
        return {"input": {"required": required, "optional": optional or {}}, "output_node": output}

    def combo(*items):
        return [list(items)]

    def number(kind, low, high):
        return [kind, {"min": low, "max": high}]

    def dynamic(options):
        return ["COMFY_DYNAMICCOMBO_V3", {"options": options}]

    encoder = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    models = ["wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"]
    codec = dynamic([{"key": "h264", "inputs": {"optional": {"encoding": dynamic([{"key": "auto", "inputs": {}}])}}}])
    return {
        "LoadImage": node({"image": combo("uploaded.png")}),
        "CLIPLoader": node({"clip_name": combo(encoder), "type": combo("wan")}, {"device": combo("cpu", "default")}),
        "UNETLoader": node({"unet_name": [models], "weight_dtype": combo("default", "fp8_e4m3fn")}),
        "VAELoader": node({"vae_name": combo("wan_2.1_vae.safetensors")}),
        "CLIPTextEncode": node({"clip": ["CLIP"], "text": ["STRING"]}),
        "ModelSamplingSD3": node({"model": ["MODEL"], "shift": number("FLOAT", 0, 100)}),
        "WanFirstLastFrameToVideo": node({
            "positive": ["CONDITIONING"], "negative": ["CONDITIONING"], "vae": ["VAE"],
            "width": number("INT", 16, 16384), "height": number("INT", 16, 16384),
            "length": number("INT", 1, 16384), "batch_size": number("INT", 1, 4096),
        }, {"start_image": ["IMAGE"], "end_image": ["IMAGE"]}),
        "KSamplerAdvanced": node({
            "model": ["MODEL"], "add_noise": combo("enable", "disable"), "noise_seed": number("INT", 0, 2**64-1),
            "steps": number("INT", 1, 10000), "cfg": number("FLOAT", 0, 100),
            "sampler_name": combo("euler"), "scheduler": combo("simple"),
            "positive": ["CONDITIONING"], "negative": ["CONDITIONING"], "latent_image": ["LATENT"],
            "start_at_step": number("INT", 0, 10000), "end_at_step": number("INT", 0, 10000),
            "return_with_leftover_noise": combo("enable", "disable"),
        }),
        "VAEDecode": node({"samples": ["LATENT"], "vae": ["VAE"]}),
        "CreateVideo": node({"images": ["IMAGE"], "fps": number("FLOAT", 1, 120)}),
        "SaveVideo": node({"video": ["VIDEO"], "filename_prefix": ["STRING"],
                           "format": dynamic([{"key": "mp4", "inputs": {"required": {"codec": codec}}}])}, output=True),
    }


class MockComfyUI:
    def __init__(self, ffmpeg_bin="ffmpeg", *, failure=None):
        self.ffmpeg_bin, self.failure = ffmpeg_bin, failure
        self.prompts, self.uploads, self.polls, self.calls = {}, [], {}, []
        self._videos = {}

    def _video(self, graph):
        frames, fps = graph["81"]["inputs"]["length"], graph["86"]["inputs"]["fps"]
        key = (frames, fps)
        if key not in self._videos:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "synthetic.mp4"
                subprocess.run([
                    self.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}",
                    "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "32", "-pix_fmt", "yuv420p", str(path),
                ], check=True, capture_output=True, timeout=120)
                self._videos[key] = path.read_bytes()
        return self._videos[key]

    def handle(self, request):
        self.calls.append((request.method, request.url.path, request.url.host))
        path = request.url.path
        expected_method = "POST" if path in {"/prompt", "/upload/image"} else "GET"
        assert request.method == expected_method
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.35.0-MOCK"}, "devices": []})
        if path == "/object_info":
            info = object_info()
            if self.failure == "missing_model":
                info["UNETLoader"]["input"]["required"]["unet_name"] = [[]]
            return httpx.Response(200, json=info)
        if path == "/upload/image":
            assert b'name="image"' in request.content and b"image/png" in request.content
            name = f"renamed-{len(self.uploads)}.png"
            self.uploads.append(name)
            return httpx.Response(200, json={"name": name, "subfolder": "server-assigned", "type": "input"})
        if path == "/prompt":
            if self.failure == "rejected":
                return httpx.Response(400, json={"error": {"type": "prompt_outputs_failed_validation"}, "node_errors": {}})
            body = json.loads(request.content)
            assert set(body) == {"prompt", "client_id"}
            graph = body["prompt"]
            assert graph["81"]["inputs"]["start_image"] == ["80", 0]
            assert graph["81"]["inputs"]["end_image"] == ["89", 0]
            for node in ("80", "89"):
                assert graph[node]["inputs"]["image"].startswith("server-assigned/renamed-")
            prompt_id = f"mock-{len(self.prompts)}"
            self.prompts[prompt_id] = graph
            if self.failure == "lost_submission":
                raise httpx.ReadError("response lost after enqueue", request=request)
            return httpx.Response(200, json={"prompt_id": prompt_id, "number": len(self.prompts), "node_errors": {}})
        if path.startswith("/history/"):
            prompt_id = path.split("/")[-1]
            self.polls[prompt_id] = self.polls.get(prompt_id, 0) + 1
            if self.failure == "timeout" or self.polls[prompt_id] == 1:
                return httpx.Response(200, json={})
            if self.failure == "execution":
                return httpx.Response(200, json={prompt_id: {"outputs": {}, "status": {"completed": False, "status_str": "error", "messages": [["execution_error", {}]]}}})
            outputs = {} if self.failure == "missing_output" else {"83": {"images": [{"filename": prompt_id + ".mp4", "subfolder": "video", "type": "output"}], "animated": [True]}}
            return httpx.Response(200, json={prompt_id: {"outputs": outputs, "status": {"completed": True, "status_str": "success", "messages": [["execution_start", {"timestamp": 1000}], ["execution_success", {"timestamp": 2000}]]}}})
        if path == "/queue":
            return httpx.Response(200, json={"queue_running": [[0, p, {}, {}, []] for p in self.prompts], "queue_pending": []})
        if path == "/view":
            assert request.url.params["subfolder"] == "video" and request.url.params["type"] == "output"
            if self.failure == "download":
                return httpx.Response(503)
            prompt_id = request.url.params["filename"].removesuffix(".mp4")
            return httpx.Response(200, content=self._video(self.prompts[prompt_id]), headers={"Content-Type": "video/mp4"})
        raise AssertionError(f"Unexpected Mock request: {request.method} {path}")
