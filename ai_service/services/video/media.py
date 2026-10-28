"""FFmpeg로 생성 클립을 연결·인코딩하고 ffprobe로 최종 영상 조건을 검증한다."""
import asyncio
from fractions import Fraction
import json
from pathlib import Path

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError

MAX_VIDEO_BYTES = 500_000_000


class Media:
    def __init__(self, settings: VideoSettings):
        self.settings = settings

    async def run(self, args: list[str]) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise VideoError("media_binary_missing") from exc
        try:
            async with asyncio.timeout(self.settings.ffmpeg_timeout):
                output, _stderr = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode != 0:
            # FFmpeg 오류에 비공개 파일명이 포함될 수 있으므로 표준 오류 원문은 기록하지 않는다.
            raise VideoError("media_process_failed", returncode=process.returncode)
        return output

    async def probe(self, path: Path) -> dict:
        raw = await self.run([
            self.settings.ffprobe_bin, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
        ])
        try:
            info = json.loads(raw)
            streams = [s for s in info["streams"] if s["codec_type"] == "video"]
            if len(streams) != 1:
                raise ValueError("video stream count")
            stream = streams[0]
            return {"width": stream["width"], "height": stream["height"], "codec": stream["codec_name"],
                    "pixel_format": stream.get("pix_fmt"), "fps": float(Fraction(stream["avg_frame_rate"])),
                    "sample_aspect_ratio": stream.get("sample_aspect_ratio"),
                    "duration_seconds": float(info["format"]["duration"]),
                    "size_bytes": path.stat().st_size, "container": info["format"]["format_name"]}
        except (ValueError, KeyError, ZeroDivisionError) as exc:
            raise VideoError("media_probe_invalid") from exc

    async def check_clip(self, path: Path, duration: float):
        info = await self.probe(path)
        if (info["width"], info["height"]) != (1280, 720) or abs(info["duration_seconds"] - duration) > 0.15:
            raise VideoError("generated_clip_invalid")
        return info

    async def finalize(self, clips: list[Path], folder: Path, duration_seconds: int) -> tuple[Path, dict]:
        # 연결 목록에는 서비스가 정한 파일명만 기록하고 사용자 입력 경로를 직접 넣지 않는다.
        if not clips or any(p.parent != folder or p.name != f"clip-{i:04d}.mp4" for i, p in enumerate(clips)):
            raise VideoError("concat_paths_invalid")
        concat = folder / "concat.txt"
        concat.write_text("".join(f"file '{p.name}'\n" for p in clips))
        temporary, final = folder / "final.partial.mp4", folder / "final.mp4"
        # 최대 180초에서도 500MB 상한에 여유가 있도록 비트레이트 버퍼 한도를 제한한다.
        maxrate = min(8_000_000, int(MAX_VIDEO_BYTES * 8 * 0.90 / duration_seconds))
        try:
            await self.run([
                self.settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "concat", "-safe", "1", "-i", str(concat), "-map", "0:v:0", "-an",
                "-vf", "scale=1280:720:flags=lanczos,setsar=1,fps=16", "-t", str(duration_seconds),
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-maxrate", str(maxrate), "-bufsize", str(maxrate * 2),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-map_metadata", "-1", str(temporary),
            ])
            info = await self.probe(temporary)
            if (info["width"], info["height"]) != (1280, 720) or info["sample_aspect_ratio"] != "1:1":
                raise VideoError("final_video_dimensions_invalid")
            if info["codec"] != "h264" or info["pixel_format"] != "yuv420p" or "mp4" not in info["container"]:
                raise VideoError("final_video_format_invalid")
            if not 0 < info["size_bytes"] <= MAX_VIDEO_BYTES:
                raise VideoError("final_video_size_invalid")
            if abs(info["duration_seconds"] - duration_seconds) > 0.07:
                raise VideoError("final_video_duration_invalid")
            temporary.replace(final)
            return final, info
        finally:
            temporary.unlink(missing_ok=True)
