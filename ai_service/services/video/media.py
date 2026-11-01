"""FFmpeg로 생성 클립을 연결·인코딩하고 ffprobe로 최종 영상 조건을 검증한다."""
import asyncio
from fractions import Fraction
import json
import math
from pathlib import Path

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError
from ai_service.services.video.music import choose_preset, render_preset

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
            audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
            return {"width": stream["width"], "height": stream["height"], "codec": stream["codec_name"],
                    "pixel_format": stream.get("pix_fmt"), "fps": float(Fraction(stream["avg_frame_rate"])),
                    "sample_aspect_ratio": stream.get("sample_aspect_ratio"),
                    "duration_seconds": float(info["format"]["duration"]),
                    "size_bytes": path.stat().st_size, "container": info["format"]["format_name"],
                    'audio_streams': len(audio), 'audio_codec': audio[0]['codec_name'] if audio else None,
                    'audio_channels': audio[0].get('channels') if audio else None}
        except (ValueError, KeyError, ZeroDivisionError) as exc:
            raise VideoError("media_probe_invalid") from exc

    async def check_clip(self, path: Path, duration: float, *, size=(1280, 720)):
        info = await self.probe(path)
        if (info["width"], info["height"]) != size or abs(info["duration_seconds"] - duration) > 0.15:
            raise VideoError("generated_clip_invalid")
        return info

    async def photo_motion(self, photo: Path, destination: Path, frames: int, index: int):
        """실제 사진에만 완만한 줌·이동을 적용한다. 생성 모델은 호출하지 않는다."""
        progress = f'on/{max(1, frames - 1)}'
        zoom = f'1.02+0.04*({progress})' if index % 2 == 0 else f'1.06-0.04*({progress})'
        # 2배 입력에서 이동해 저해상도 좌표 반올림에 따른 떨림을 줄인다.
        pan = f'(iw-iw/zoom)*(0.35+0.3*({progress}))'
        await self.run([
            self.settings.ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', str(photo), '-vf',
            f"scale=2560:1440:flags=lanczos,zoompan=z='{zoom}':x='{pan}':"
            f"y='(ih-ih/zoom)/2':d={frames}:s=1280x720:fps=16,setsar=1",
            '-frames:v', str(frames), '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(destination),
        ])
        return await self.check_clip(destination, frames / 16)

    async def remove_generated_overlaps(self, video: Path, references: list[Path], guide_indices: list[int]):
        import cv2
        import numpy as np
        from ai_service.services.video.editing import find_overlap_ranges
        raw=await self.run([self.settings.ffmpeg_bin,'-v','error','-i',str(video),'-vf','scale=96:54',
                            '-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
        frames=np.frombuffer(raw,dtype=np.uint8).reshape(-1,54,96,3)
        photos=np.stack([cv2.cvtColor(cv2.resize(cv2.imread(str(p)),(96,54),interpolation=cv2.INTER_AREA),cv2.COLOR_BGR2RGB)
                         for p in references])
        result=await asyncio.to_thread(find_overlap_ranges,frames,photos,guide_indices)
        if not result['applied']:return result
        filters='+'.join(f'between(n,{a},{b})' for a,b in result['ranges_frames'])
        temporary=video.parent/'editing.partial.mp4'
        try:
            await self.run([self.settings.ffmpeg_bin,'-hide_banner','-loglevel','error','-nostdin','-y',
                '-i',str(video),'-vf',f"select='not({filters})',setpts=N/(24*TB)",'-an','-fps_mode','passthrough',
                '-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',str(temporary)])
            result['edited_duration_seconds']=(result['source_frames']-result['removed_frames'])/24
            await self.check_clip(temporary,result['edited_duration_seconds'],size=(1024,576))
            original=video.parent/'generated-0000.mp4'
            if original.exists():raise VideoError('generated_original_already_exists')
            video.rename(original);temporary.replace(video)
            result['original_file']=str(original)
            return result
        finally:temporary.unlink(missing_ok=True)

    async def assemble_memory_film(self,takes,folder,*,duration_seconds=30,fps=24,audio=None,storyboard=None):
        """Edit reviewed AI footage at its original speed; no photos, holds or duration stretching."""
        if (not takes or sum(t.frame_count for t in takes)!=duration_seconds*fps
                or any(type(t.start_frame) is not int or type(t.frame_count) is not int
                       or t.start_frame<0 or t.frame_count<=0 for t in takes)):
            raise VideoError('memory_film_timing_invalid')
        info={}
        for take in takes:
            if take.source not in info:info[take.source]=await self.probe(take.source)
            native=info[take.source]
            if abs(native['fps']-fps)>0.01 or take.start_frame+take.frame_count>round(native['duration_seconds']*fps)+1:
                raise VideoError('memory_film_range_invalid')
            if take.crop:
                x,y,w,h=take.crop
                if min(x,y)<0 or min(w,h)<=0 or x+w>native['width'] or y+h>native['height']:
                    raise VideoError('memory_film_crop_invalid')
        folder.mkdir(parents=True,exist_ok=True)
        clips=[];ranges=[];position=0
        for i,take in enumerate(takes):
            clip=folder/f'clip-{i:04d}.mp4'
            framing=''
            if take.crop:
                x,y,w,h=take.crop;framing=f',crop={w}:{h}:{x}:{y}'
            await self.run([self.settings.ffmpeg_bin,'-hide_banner','-loglevel','error','-nostdin','-y',
                '-i',str(take.source),'-vf',f'trim=start_frame={take.start_frame}:end_frame={take.start_frame+take.frame_count},setpts=PTS-STARTPTS{framing},scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setsar=1',
                '-frames:v',str(take.frame_count),'-an','-c:v','libx264','-preset','fast','-crf','16','-pix_fmt','yuv420p',str(clip)])
            clips.append(clip);ranges.append({'source':str(take.source),'start_frame':take.start_frame,
                'frame_count':take.frame_count,'memory_role':take.memory_role,'film_start_frame':position,'crop':take.crop})
            position+=take.frame_count
        output,media=await self.finalize(clips,folder,duration_seconds,audio=audio,memory_style='WARM',fps=fps,storyboard=storyboard)
        report={'status':'rendered_pending_editorial_review','output_file':str(output),'media':media,
                'duration_seconds':duration_seconds,'fps':fps,'takes':ranges,'retiming_applied':False,
                'still_photo_insertion':False,'source_kind':'generated_ai_video','music_dynamics_applied':bool(storyboard and storyboard.emotional_arc)}
        (folder/'film-edit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return report

    async def prepare_music(self, selection, folder: Path, duration: int, style: str, context: str):
        if selection.file is not None:
            source = selection.file.resolve()
            if not source.is_file() or source.stat().st_size > 100_000_000:
                raise VideoError('music_missing_or_too_large')
            raw = await self.run([self.settings.ffprobe_bin, '-v', 'error', '-show_streams',
                                  '-show_format', '-of', 'json', str(source)])
            try:
                data = json.loads(raw)
                length = float(data['format']['duration'])
                if not any(s.get('codec_type') == 'audio' for s in data['streams']):
                    raise ValueError('no audio')
                if not math.isfinite(length) or selection.start_seconds >= length:
                    raise ValueError('invalid audio start')
            except (ValueError, KeyError, TypeError) as exc:
                raise VideoError('music_invalid') from exc
            path = folder / 'music-source.wav'
            await self.run([
                self.settings.ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-ss', str(selection.start_seconds), '-i', str(source), '-map', '0:a:0', '-vn',
                '-t', str(duration), '-ac', '2', '-ar', '44100', '-c:a', 'pcm_s16le', str(path),
            ])
            return path, {'source': 'USER_FILE', 'start_seconds': selection.start_seconds, 'volume': selection.volume}
        preset = choose_preset(selection, style, context)
        if preset == 'NONE':
            return None, {'source': 'NONE'}
        path = folder / 'music-source.wav'
        await asyncio.to_thread(render_preset, path, preset, duration)
        return path, {'source': 'BUILTIN_SYNTHESIZED', 'requested_preset': selection.preset,
                      'selected_preset': preset, 'volume': selection.volume}

    async def finalize(self, clips: list[Path], folder: Path, duration_seconds: int, *,
                       audio: Path | None = None, volume: float = 0.65,
                       memory_style: str = 'WARM', fps: int = 16, storyboard=None) -> tuple[Path, dict]:
        # 연결 목록에는 서비스가 정한 파일명만 기록하고 사용자 입력 경로를 직접 넣지 않는다.
        if not clips or any(p.parent != folder or p.name != f"clip-{i:04d}.mp4" for i, p in enumerate(clips)):
            raise VideoError("concat_paths_invalid")
        concat = folder / "concat.txt"
        concat.write_text("".join(f"file '{p.name}'\n" for p in clips))
        temporary, final = folder / "final.partial.mp4", folder / "final.mp4"
        # 최대 180초에서도 500MB 상한에 여유가 있도록 비트레이트 버퍼 한도를 제한한다.
        maxrate = min(8_000_000, int(MAX_VIDEO_BYTES * 8 * 0.90 / duration_seconds))
        looks = {
            'WARM': 'eq=contrast=1.035:saturation=1.07:brightness=0.008,colorbalance=rs=0.02:bs=-0.015',
            'LIVELY': 'eq=contrast=1.04:saturation=1.12:brightness=0.01',
            'CINEMATIC': 'eq=contrast=1.06:saturation=1.03,vignette=angle=PI/9',
        }
        video_filter = f'scale=1280:720:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:720,setsar=1,fps={fps},{looks[memory_style]}'
        audio_input = ['-stream_loop', '-1', '-i', str(audio)] if audio else []
        overlay_inputs=[]
        video_map=['-map','0:v:0'];video_options=['-vf',video_filter]
        if storyboard and (storyboard.title or storyboard.closing_line):
            from ai_service.services.video.titles import render_titles
            titles=await asyncio.to_thread(render_titles,folder,storyboard)
            chain=[f'[0:v]{video_filter},fade=t=in:st=0:d=0.2,fade=t=out:st={duration_seconds-0.45}:d=0.45[base]']
            previous='base'
            for i,(name,path) in enumerate(titles):
                overlay_inputs.extend(['-loop','1','-framerate',str(fps),'-i',str(path)])
                index=i+(2 if audio else 1)
                start,end=(0.35,min(2.8,duration_seconds-0.5)) if name.startswith('opening') else (max(0,duration_seconds-4),duration_seconds-0.5)
                chain.append(f'[{index}:v]format=rgba,fade=t=in:st={start}:d=0.3:alpha=1,fade=t=out:st={end-0.3}:d=0.3:alpha=1[caption{i}]')
                chain.append(f'[{previous}][caption{i}]overlay=0:0:enable=\'between(t,{start},{end})\'[captioned{i}]')
                previous=f'captioned{i}'
            video_map=['-map',f'[{previous}]'];video_options=['-filter_complex',';'.join(chain)]
        fade = min(0.8, duration_seconds / 4)
        music_dynamics=''
        if storyboard and storyboard.emotional_arc:
            a,b,c=duration_seconds*0.1,duration_seconds*0.6,duration_seconds*0.8
            envelope=(f'if(lt(t,{a}),0.45+0.25*t/{a},'
                      f'if(lt(t,{b}),0.7+0.35*(t-{a})/{b-a},'
                      f'if(lt(t,{c}),1.05,1.05-0.45*(t-{c})/{duration_seconds-c})))')
            music_dynamics=f"volume='{envelope}':eval=frame,"
        audio_options = ([
            '-map', '1:a:0', '-af',
            f'loudnorm=I=-18:TP=-2:LRA=7,volume={volume},{music_dynamics}afade=t=in:st=0:d={fade},'
            f'afade=t=out:st={duration_seconds - fade}:d={fade},alimiter=limit=0.89:level=0',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '44100', '-ac', '2',
        ] if audio else ['-an'])
        try:
            await self.run([
                self.settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "concat", "-safe", "1", "-i", str(concat), *audio_input,*overlay_inputs, *video_map, *audio_options,
                *video_options, "-t", str(duration_seconds),
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
            if audio and (info['audio_streams'] != 1 or info['audio_codec'] != 'aac' or info['audio_channels'] != 2):
                raise VideoError('final_video_audio_invalid')
            temporary.replace(final)
            return final, info
        finally:
            temporary.unlink(missing_ok=True)
