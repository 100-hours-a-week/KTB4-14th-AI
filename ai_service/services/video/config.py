"""영상 생성에 필요한 환경변수·접속 정보·작업 경로·실행 제한을 불러오고 검증한다."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import math
from urllib.parse import urlsplit

from dotenv import dotenv_values

from ai_service.core.config import ENV_FILE


@dataclass(frozen=True)
class VideoSettings:
    endpoint_mode: str = "static"
    comfyui_url: str = field(default="http://127.0.0.1:8188", repr=False)
    comfyui_token: str | None = field(default=None, repr=False)
    runpod_api_key: str | None = field(default=None, repr=False)
    runpod_prefix: str = "audigo-video-"
    runpod_pod_id: str | None = None
    runpod_port: int = 8188
    discovery_ttl: float = 30.0
    workspace: Path = Path("/workspace/audigo-video")
    request_timeout: float = 30.0
    prompt_timeout: float = 3600.0
    job_timeout: float = 43200.0
    poll_interval: float = 2.0
    ffmpeg_timeout: float = 1800.0
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    high_model: str = "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
    low_model: str = "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
    text_encoder: str = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    vae: str = "wan_2.1_vae.safetensors"
    text_encoder_device: str = "cpu"
    hourly_usd: float = 1.11
    max_input_bytes: int = 25_000_000
    max_clip_bytes: int = 500_000_000

    def __post_init__(self):
        if self.endpoint_mode not in {"static", "runpod"}:
            raise ValueError("VIDEO_ENDPOINT_MODE must be static or runpod")
        if self.endpoint_mode == "static":
            url = urlsplit(self.comfyui_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("COMFYUI_URL must be an HTTP(S) base URL without credentials/query")
        if not self.runpod_prefix or not 1 <= self.runpod_port <= 65535:
            raise ValueError("Invalid RunPod prefix/port")
        for name in ("request_timeout", "prompt_timeout", "job_timeout", "poll_interval", "ffmpeg_timeout", "hourly_usd"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not math.isfinite(self.discovery_ttl) or self.discovery_ttl < 0 or self.text_encoder_device not in {"cpu", "default"}:
            raise ValueError("Invalid discovery TTL/text encoder device")
        if min(self.max_input_bytes, self.max_clip_bytes) <= 0:
            raise ValueError("Input/download size limits must be positive")

    @classmethod
    def from_env(cls, env_file: Path = ENV_FILE):
        values = {**dotenv_values(env_file), **os.environ}
        mapping = {
            "endpoint_mode": ("VIDEO_ENDPOINT_MODE", str),
            "comfyui_url": ("COMFYUI_URL", str),
            "comfyui_token": ("COMFYUI_API_TOKEN", str),
            "runpod_api_key": ("RUNPOD_API_KEY", str),
            "runpod_prefix": ("RUNPOD_POD_PREFIX", str),
            "runpod_pod_id": ("RUNPOD_POD_ID", str),
            "runpod_port": ("RUNPOD_COMFYUI_PORT", int),
            "discovery_ttl": ("VIDEO_DISCOVERY_TTL_SECONDS", float),
            "workspace": ("VIDEO_WORKSPACE", Path),
            "request_timeout": ("VIDEO_HTTP_TIMEOUT_SECONDS", float),
            "prompt_timeout": ("VIDEO_PROMPT_TIMEOUT_SECONDS", float),
            "job_timeout": ("VIDEO_JOB_TIMEOUT_SECONDS", float),
            "poll_interval": ("VIDEO_POLL_SECONDS", float),
            "ffmpeg_timeout": ("VIDEO_FFMPEG_TIMEOUT_SECONDS", float),
            "ffmpeg_bin": ("FFMPEG_BIN", str),
            "ffprobe_bin": ("FFPROBE_BIN", str),
            "high_model": ("VIDEO_HIGH_MODEL", str),
            "low_model": ("VIDEO_LOW_MODEL", str),
            "text_encoder": ("VIDEO_TEXT_ENCODER", str),
            "vae": ("VIDEO_VAE", str),
            "text_encoder_device": ("VIDEO_TEXT_ENCODER_DEVICE", str),
            "hourly_usd": ("VIDEO_GPU_HOURLY_USD", float),
        }
        return cls(**{key: cast(values[env]) for key, (env, cast) in mapping.items() if values.get(env)})
