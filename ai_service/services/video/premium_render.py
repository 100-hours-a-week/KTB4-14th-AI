"""Finish reviewed AI takes as a restrained travel campaign film.

This renderer does not create detail, repair generated geometry or certify source
quality. Its 1080p output may contain scaled sources; native dimensions are kept
in the report so export resolution cannot be mistaken for generation resolution.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import math
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ai_service.services.video.editing import FilmTake
from ai_service.services.video.errors import VideoError
from ai_service.services.video.media import Media


def _font(text: str, size: int, font_path: Path | None, font_index: int | None):
    candidates = ([Path(font_path)] if font_path else [
        Path('/System/Library/Fonts/AppleSDGothicNeo.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/noto/NotoSansKR-Medium.ttf'),
    ])
    if text.isascii() and not font_path:
        candidates.append(Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
    path = next((path for path in candidates if path.is_file()), None)
    if path is None:
        raise VideoError('premium_caption_font_missing')
    index = font_index if font_index is not None else (4 if path.name == 'AppleSDGothicNeo.ttc' else 0)
    try:
        return ImageFont.truetype(str(path), size, index=index), str(path), index
    except (OSError, ValueError) as error:
        raise VideoError('premium_caption_font_invalid') from error


def _caption(
    text: str,
    path: Path,
    size: tuple[int, int],
    *,
    opening: bool,
    color: tuple[int, int, int, int],
    font_path: Path | None,
    font_index: int | None,
) -> dict:
    width, height = size
    # A two-line opening sits in sky/sea negative space, not over the subject.
    lines = text.replace(', ', ',\n', 1) if opening else text
    point_size = max(14, round(width * (0.039 if opening else 0.044)))
    font, used_font, used_index = _font(lines, point_size, font_path, font_index)
    scratch = Image.new('RGBA', size)
    draw = ImageDraw.Draw(scratch)
    spacing = max(4, round(point_size * 0.21))
    while draw.multiline_textbbox((0, 0), lines, font=font, spacing=spacing)[2] > width * 0.77:
        point_size -= 1
        if point_size < 10:
            raise VideoError('premium_caption_too_long')
        font, _, _ = _font(lines, point_size, font_path, font_index)
    x, y = round(width * 0.086), round(height * (0.19 if opening else 0.23))
    # Only letters cast a soft shadow: there is no full-screen tint or dark card.
    shadow = Image.new('RGBA', size)
    ImageDraw.Draw(shadow).multiline_text(
        (x, y + max(1, round(height * 0.002))), lines, font=font,
        spacing=spacing, fill=(5, 19, 27, 125), stroke_width=1,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, width / 500)))
    image = Image.alpha_composite(scratch, shadow)
    ImageDraw.Draw(image).multiline_text((x, y), lines, font=font, spacing=spacing, fill=color)
    image.save(path)
    return {'text': text, 'image': str(path), 'position': [x, y],
            'font_size': point_size, 'font_path': used_font, 'font_index': used_index,
            'color_rgba': list(color), 'full_screen_tint': False}


async def _measure_score(media: Media, music: Path, filters: str) -> dict[str, float]:
    """Measure the exact edited score before the delivery normalization pass."""
    try:
        process = await asyncio.create_subprocess_exec(
            media.settings.ffmpeg_bin, '-hide_banner', '-nostdin', '-i', str(music),
            '-vn', '-af', filters + ',loudnorm=I=-16:TP=-1:LRA=11:print_format=json',
            '-f', 'null', '-', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise VideoError('media_binary_missing') from error
    try:
        async with asyncio.timeout(media.settings.ffmpeg_timeout):
            _, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise VideoError('media_process_failed', returncode=process.returncode)
    # Diagnostic stderr can contain private input paths; keep only numeric meter data.
    raw = stderr.decode('utf-8', errors='replace')
    try:
        data = json.loads(raw[raw.rfind('{'):raw.rfind('}') + 1])
        measurements = {name: float(data[name]) for name in
                        ('input_i', 'input_tp', 'input_lra', 'input_thresh', 'target_offset')}
        if not all(math.isfinite(value) for value in measurements.values()):
            raise ValueError('silent or unmeasurable score')
    except (KeyError, ValueError, TypeError) as error:
        raise VideoError('premium_music_loudness_invalid') from error
    return measurements


async def render_premium_film(
    media: Media,
    takes: Sequence[FilmTake],
    output_folder: Path,
    music_path: Path | None,
    *,
    output_size: tuple[int, int] = (1920, 1080),
    music_volume: float = 1.0,
    target_seconds: float = 30,
    fps: int = 24,
    opening_title: str | None = '제주에서, 조금 더 행복하게.',
    closing_title: str | None = '좋아서, 다시 제주.',
    opening_window: tuple[float, float] = (0.8, 3.1),
    closing_window: tuple[float, float] | None = None,
    opening_caption_path: Path | None = None,
    closing_caption_path: Path | None = None,
    caption_color: tuple[int, int, int, int] = (250, 249, 244, 255),
    font_path: Path | None = None,
    font_index: int | None = None,
) -> dict:
    """Render exact native-rate take ranges, hard cuts, two captions and a score.

    ``FilmTake`` ranges are in native source frames. Sources must match ``fps``;
    mismatched rates are rejected rather than silently changing motion speed.
    Explicit caption files must be RGBA images at ``output_size``. Captions can
    be disabled by setting both the title and corresponding image path to None.
    Existing deliverables are never overwritten.
    """
    if (type(fps) is not int or not 1 <= fps <= 60
            or not isinstance(target_seconds, (int, float)) or isinstance(target_seconds, bool)
            or not math.isfinite(target_seconds) or not 0 < target_seconds <= 180
            or len(output_size) != 2
            or any(type(value) is not int or value < 64 or value % 2 for value in output_size)):
        raise VideoError('premium_output_settings_invalid')
    total_frames = round(target_seconds * fps)
    if (abs(target_seconds * fps - total_frames) > 1e-6 or not takes
            or any(type(take.start_frame) is not int or type(take.frame_count) is not int
                   or take.start_frame < 0 or take.frame_count <= 0 for take in takes)
            or sum(take.frame_count for take in takes) != total_frames):
        raise VideoError('premium_film_timing_invalid')
    folder = Path(output_folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    final, temporary = folder / 'premium-final.mp4', folder / 'premium-final.partial.mp4'
    if final.exists() or temporary.exists():
        raise VideoError('premium_output_already_exists')
    if (len(caption_color) != 4 or any(type(c) is not int or not 0 <= c <= 255 for c in caption_color)):
        raise VideoError('premium_caption_color_invalid')

    source_paths = list(dict.fromkeys(Path(take.source).resolve() for take in takes))
    source_info = {}
    for path in source_paths:
        if not path.is_file():
            raise VideoError('premium_source_missing')
        info = await media.probe(path)
        if abs(info['fps'] - fps) > 0.01:
            raise VideoError('premium_source_fps_mismatch')
        source_info[path] = info
    for take in takes:
        info = source_info[Path(take.source).resolve()]
        if take.start_frame + take.frame_count > round(info['duration_seconds'] * fps) + 1:
            raise VideoError('premium_take_range_invalid')
        if take.crop is not None:
            if (len(take.crop) != 4 or any(type(value) is not int for value in take.crop)):
                raise VideoError('premium_take_crop_invalid')
            x, y, width, height = take.crop
            if min(x, y) < 0 or min(width, height) <= 0 or x + width > info['width'] or y + height > info['height']:
                raise VideoError('premium_take_crop_invalid')

    if not 0 < music_volume <= 1:
        raise VideoError('premium_music_volume_invalid')
    music = Path(music_path).resolve() if music_path is not None else None
    audio_duration = None
    if music is not None:
        if not music.is_file():
            raise VideoError('premium_music_missing')
        raw_audio_info = await media.run([
            media.settings.ffprobe_bin, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(music),
        ])
        try:
            audio_info = json.loads(raw_audio_info)
            audio_streams = [stream for stream in audio_info['streams'] if stream.get('codec_type') == 'audio']
            audio_duration = float(audio_info['format']['duration'])
            if not audio_streams or not math.isfinite(audio_duration) or audio_duration < target_seconds - 0.05:
                raise ValueError('insufficient music')
        except (KeyError, TypeError, ValueError) as error:
            raise VideoError('premium_music_invalid_or_too_short') from error

    width, height = output_size
    args = [media.settings.ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n']
    for path in source_paths:
        args.extend(['-i', str(path)])
    audio_index = len(source_paths)
    if music is not None:
        args.extend(['-i', str(music)])

    graph = []
    counts = Counter(Path(take.source).resolve() for take in takes)
    branch_queues = {}
    for index, path in enumerate(source_paths):
        if counts[path] == 1:
            branch_queues[path] = [f'{index}:v:0']
        else:
            names = [f'source{index}_{branch}' for branch in range(counts[path])]
            graph.append(f'[{index}:v:0]split={len(names)}' + ''.join(f'[{name}]' for name in names))
            branch_queues[path] = names

    ranges, start_frame = [], 0
    for index, take in enumerate(takes):
        path = Path(take.source).resolve()
        source = branch_queues[path].pop(0)
        filters = [f'trim=start_frame={take.start_frame}:end_frame={take.start_frame + take.frame_count}',
                   'setpts=PTS-STARTPTS']
        if take.crop is not None:
            x, y, crop_width, crop_height = take.crop
            filters.append(f'crop={crop_width}:{crop_height}:{x}:{y}')
        filters.extend([f'scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos',
                        f'crop={width}:{height}', 'setsar=1', 'format=yuv420p', 'settb=AVTB'])
        graph.append(f'[{source}]' + ','.join(filters) + f'[take{index}]')
        ranges.append({'source': str(path), 'start_frame': take.start_frame, 'frame_count': take.frame_count,
                       'film_start_frame': start_frame, 'memory_role': take.memory_role,
                       'crop': list(take.crop) if take.crop else None})
        start_frame += take.frame_count
    graph.append(''.join(f'[take{index}]' for index in range(len(takes))) +
                 f'concat=n={len(takes)}:v=1:a=0,eq=contrast=1.025:saturation=1.045:gamma=1.01[graded]')
    previous, captions = 'graded', []
    closing_window = closing_window or (max(0, target_seconds - 4), max(0, target_seconds - 1))
    next_input = audio_index + (1 if music is not None else 0)
    for name, title, explicit_path, window, opening in [
        ('opening', opening_title, opening_caption_path, opening_window, True),
        ('closing', closing_title, closing_caption_path, closing_window, False),
    ]:
        if title is None and explicit_path is None:
            continue
        if (len(window) != 2 or any(not math.isfinite(value) for value in window)
                or not 0 <= window[0] < window[1] <= target_seconds):
            raise VideoError('premium_caption_window_invalid')
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 100):
            raise VideoError('premium_caption_text_invalid')
        if explicit_path:
            image_path = Path(explicit_path).resolve()
            try:
                with Image.open(image_path) as image:
                    if image.size != output_size or image.mode != 'RGBA':
                        raise ValueError('caption size/mode')
                    image.verify()
            except (OSError, ValueError) as error:
                raise VideoError('premium_caption_image_invalid') from error
            caption = {'image': str(image_path), 'source': 'provided_rgba', 'text': title}
        else:
            image_path = folder / f'{name}-caption.png'
            if image_path.exists():
                raise VideoError('premium_caption_already_exists')
            caption = await asyncio.to_thread(_caption, title, image_path, output_size,
                                             opening=opening, color=caption_color,
                                             font_path=font_path, font_index=font_index)
        start, end = window
        fade = min(0.25, (end - start) / 3)
        args.extend(['-loop', '1', '-framerate', str(fps), '-i', str(image_path)])
        graph.append(f'[{next_input}:v]format=rgba,fade=t=in:st={start}:d={fade}:alpha=1,'
                     f'fade=t=out:st={end-fade}:d={fade}:alpha=1[caption{next_input}]')
        graph.append(f'[{previous}][caption{next_input}]overlay=0:0:enable=\'between(t,{start},{end})\'[layer{next_input}]')
        previous = f'layer{next_input}'
        caption.update(window_seconds=[start, end], alpha_fade_seconds=fade)
        captions.append(caption)
        next_input += 1

    loudness = None
    if music is not None:
        fade_out = min(0.5, target_seconds / 8)
        score_filters = (f'atrim=start=0:end={target_seconds},asetpts=PTS-STARTPTS,'
                         f'afade=t=in:st=0:d=0.08,'
                         f'afade=t=out:st={target_seconds-fade_out}:d={fade_out}')
        loudness = await _measure_score(media, music, score_filters)
        # Request measured linear gain to preserve the musical envelope when peak and
        # range constraints allow it. FFmpeg may fall back to dynamic normalization.
        normalization = (f'loudnorm=I=-16:TP=-1:LRA=11:measured_I={loudness["input_i"]}:'
                         f'measured_TP={loudness["input_tp"]}:measured_LRA={loudness["input_lra"]}:'
                         f'measured_thresh={loudness["input_thresh"]}:'
                         f'offset={loudness["target_offset"]}:linear=true')
        graph.append(f'[{audio_index}:a:0]{score_filters},{normalization},volume={music_volume},'
                     'alimiter=limit=0.89125:level=0:latency=1[audio]')
    filter_graph = ';'.join(graph)
    (folder / 'premium-filtergraph.txt').write_text(filter_graph + '\n')
    args.extend(['-filter_complex_threads', '2', '-filter_complex', filter_graph,
                 '-map', f'[{previous}]', *(['-map', '[audio]'] if music is not None else ['-an']), '-frames:v', str(total_frames),
                 '-t', str(target_seconds), '-fps_mode', 'passthrough',
                 '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-profile:v', 'high',
                 '-maxrate', '20M', '-bufsize', '40M', '-threads', '8', '-pix_fmt', 'yuv420p',
                 '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv',
                 '-c:a', 'aac', '-b:a', '256k', '-ar', '48000', '-ac', '2',
                 '-movflags', '+faststart', '-map_metadata', '-1', str(temporary)])
    try:
        await media.run(args)
        output_info = await media.probe(temporary)
        if ((output_info['width'], output_info['height']) != output_size
                or abs(output_info['duration_seconds'] - target_seconds) > 0.075
                or abs(output_info['fps'] - fps) > 0.01
                or output_info['codec'] != 'h264' or output_info['pixel_format'] != 'yuv420p'
                or (music is not None and (output_info['audio_codec'] != 'aac' or output_info['audio_channels'] != 2))
                or (music is None and output_info['audio_streams'] != 0)
                or output_info['size_bytes'] <= 0 or output_info['size_bytes'] > 500_000_000):
            raise VideoError('premium_output_invalid')
        temporary.replace(final)
    finally:
        temporary.unlink(missing_ok=True)
    report = {
        'status': 'rendered_pending_visual_and_audio_review', 'output_file': str(final), 'media': output_info,
        'duration_seconds': target_seconds, 'fps': fps, 'frame_count': total_frames, 'takes': ranges,
        'native_sources': [{'path': str(path), 'width': info['width'], 'height': info['height'],
                            'fps': info['fps'], 'duration_seconds': info['duration_seconds']}
                           for path, info in source_info.items()],
        'native_source_sizes': sorted({(info['width'], info['height']) for info in source_info.values()}),
        'all_sources_at_least_output_resolution': all(info['width'] >= width and info['height'] >= height for info in source_info.values()),
        'retiming_applied': False, 'frame_interpolation_applied': False, 'still_photo_insertion': False,
        'hard_cuts': True, 'black_opening_added': False, 'video_fade_added': False,
        'grade': 'Neutral SDR: contrast 1.025, saturation 1.045, gamma 1.01; no yellow cast, vignette or added grain.',
        'captions': captions, 'music_file': str(music) if music is not None else None, 'music_volume': music_volume, 'music_source_duration_seconds': audio_duration,
        'audio_targets': {'integrated_lufs': round(-16 + 20 * math.log10(music_volume), 2) if music is not None else None, 'true_peak_dbtp': -1, 'loudness_range_lu': 11,
                          'measured_after_encode': False, 'sample_rate': 48000, 'channels': 2},
        'audio_normalization': {'method': 'measured_two_pass', 'linear_gain_requested': True,
                                'score_measurements': loudness},
        'processing_limits': 'Scaling and finishing do not reconstruct detail or repair AI geometry. Visual and listening review remain required.',
    }
    (folder / 'premium-render.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report
