"""자동 작곡과 대체 배경음악을 준비하고 파형과 길이를 검증한다."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from ai_service.clients.comfyui import ComfyUI
from ai_service.services.video.ace_music import (
    ace_music_asset_from_history, ace_music_provenance,
    build_ace_music_workflow, validate_ace_music_workflow,
)
from ai_service.services.video.errors import VideoError
from ai_service.services.video.media import Media
from ai_service.services.video.music import choose_preset, render_preset


SAMPLE_RATE = 48000
WINDOW_SECONDS = 0.1


@dataclass(frozen=True)
class ScorePolicy:
    """Measured rejection thresholds, not a perceptual quality score."""
    padding_seconds: int = 10
    max_attempts: int = 2
    timeout_seconds: float = 600
    minimum_active_fraction: float = 0.9
    maximum_leading_quiet_seconds: float = 0.5
    maximum_trailing_quiet_seconds: float = 1.5
    # ACE takes often breathe for about a second before the main section; 0.5 s rejected every take measured
    # on 2026-11-04 (pauses of 1.1 s and 1.8 s) although level, length and ending all passed.
    maximum_internal_quiet_seconds: float = 1.5
    maximum_clipped_sample_fraction: float = 0.0001
    minimum_ending_drop_db: float = 6

    def __post_init__(self):
        numbers = [self.padding_seconds, self.timeout_seconds,
                   self.maximum_leading_quiet_seconds, self.maximum_trailing_quiet_seconds,
                   self.maximum_internal_quiet_seconds, self.minimum_ending_drop_db]
        if (type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 2
                or any(not math.isfinite(value) or value < 0 for value in numbers)
                or self.timeout_seconds <= 0
                or not 0 < self.minimum_active_fraction <= 1
                or not 0 <= self.maximum_clipped_sample_fraction < 1):
            raise ValueError('invalid score policy')


def _save(path: Path, data: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def _db(value: float) -> float:
    return 20 * math.log10(max(value, 1e-12))


def audio_activity(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> dict:
    """Measure 100ms energy, clipping and quiet runs from decoded audio.

    The adaptive threshold distinguishes low-level tails from long dead audio;
    activity can also be noise and is never labelled musical/perceptual quality.
    """
    data = np.asarray(samples, dtype=np.float32)
    if (type(sample_rate) is not int or sample_rate <= 0 or data.ndim not in (1, 2)
            or not data.size or not np.isfinite(data).all()):
        raise VideoError('score_audio_samples_invalid')
    if data.ndim == 1:
        data = data[:, None]
    block = max(1, round(sample_rate * WINDOW_SECONDS))
    energies = np.asarray([
        float(np.sqrt(np.mean(data[start:start + block].astype(np.float64) ** 2)))
        for start in range(0, len(data), block)
    ])
    rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
    levels = np.asarray([_db(float(energy)) for energy in energies])
    threshold = max(-45.0, float(levels.max()) - 35)
    active = levels > threshold
    indices = np.flatnonzero(active)
    first = int(indices[0]) if len(indices) else len(active)
    last = int(indices[-1]) if len(indices) else -1
    internal = active[first:last + 1]
    longest, current = 0, 0
    for value in internal:
        current = 0 if value else current + 1
        longest = max(longest, current)
    duration = len(data) / sample_rate
    return {
        'duration_seconds': duration,
        'sample_rate': sample_rate,
        'channels': data.shape[1],
        'rms_dbfs': round(_db(rms), 3),
        'sample_peak_dbfs': round(_db(float(np.abs(data).max())), 3),
        'clipped_sample_fraction': float(np.mean(np.abs(data) >= 0.999)),
        'active_threshold_dbfs': round(threshold, 3),
        'active_fraction': float(np.mean(active)),
        'first_active_seconds': min(duration, first * WINDOW_SECONDS) if len(indices) else None,
        'last_active_seconds': min(duration, (last + 1) * WINDOW_SECONDS) if len(indices) else None,
        'leading_quiet_seconds': min(duration, first * WINDOW_SECONDS),
        'trailing_quiet_seconds': max(0, duration - (last + 1) * WINDOW_SECONDS),
        'longest_internal_quiet_seconds': longest * WINDOW_SECONDS,
        'one_tenth_second_rms_dbfs': [round(float(level), 3) for level in levels],
        'measurement_note': 'Energy/activity is not a music classifier. Sample peak is not true peak.',
    }


def select_score_excerpt(samples: np.ndarray, duration_seconds: float, *,
                         sample_rate: int = SAMPLE_RATE, policy: ScorePolicy | None = None) -> dict:
    """Select one continuous requested-length excerpt near the natural energy tail.

    Never concatenate, repeat, pad or stretch a short composition. A candidate
    whose active extent ends too early is rejected even if its container is long.
    The ending gate measures energy decay, not harmonic/chord resolution.
    """
    policy = policy or ScorePolicy()
    if (not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool)
            or not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= 180):
        raise VideoError('score_duration_invalid')
    data = np.asarray(samples, dtype=np.float32)
    source = audio_activity(data, sample_rate)
    wanted = round(duration_seconds * sample_rate)
    if len(data) < wanted:
        raise VideoError('score_audio_too_short', available_seconds=source['duration_seconds'])
    if source['rms_dbfs'] < -50 or source['last_active_seconds'] is None:
        raise VideoError('score_audio_silent')
    active_span = source['last_active_seconds'] - source['first_active_seconds']
    if active_span < duration_seconds - policy.maximum_trailing_quiet_seconds:
        raise VideoError('score_composition_ends_early', active_span_seconds=round(active_span, 3))
    if source['clipped_sample_fraction'] > policy.maximum_clipped_sample_fraction:
        raise VideoError('score_audio_clipped', clipped_fraction=source['clipped_sample_fraction'])

    # Tail offsets are a search over complete contiguous takes, not user-specific
    # offsets. A short final reverb decay is preferred over an abrupt interior cut.
    candidates = []
    endings = sorted({min(len(data), round((source['last_active_seconds'] + tail) * sample_rate))
                      for tail in (0.2, 0.4, 0.6, 0.8, 1.0)})
    for end in endings:
        start = end - wanted
        if start < 0:
            continue
        excerpt = data[start:end]
        activity = audio_activity(excerpt, sample_rate)
        if (activity['active_fraction'] < policy.minimum_active_fraction
                or activity['leading_quiet_seconds'] > policy.maximum_leading_quiet_seconds + 1e-6
                or activity['trailing_quiet_seconds'] > policy.maximum_trailing_quiet_seconds + 1e-6
                or activity['longest_internal_quiet_seconds'] > policy.maximum_internal_quiet_seconds + 1e-6):
            continue
        mono_energy = np.mean(excerpt.astype(np.float64) ** 2, axis=1) if excerpt.ndim == 2 else excerpt.astype(np.float64) ** 2
        final = float(np.sqrt(np.mean(mono_energy[-round(0.6 * sample_rate):])))
        reference = mono_energy[max(0, len(excerpt) - round(8 * sample_rate)):
                                max(1, len(excerpt) - round(3 * sample_rate))]
        before = float(np.sqrt(np.mean(reference)))
        drop = _db(before) - _db(final)
        if drop < policy.minimum_ending_drop_db:
            continue
        # Preserve a small audible tail without preferring additional silence.
        tail_score = -abs(activity['trailing_quiet_seconds'] - 0.6)
        candidates.append((tail_score, activity['active_fraction'], -start, {
            'start_seconds': start / sample_rate, 'end_seconds': end / sample_rate,
            'start_sample': start, 'end_sample_exclusive': end, 'sample_rate': sample_rate,
            'duration_seconds': wanted / sample_rate, 'activity': activity,
            'ending_energy_drop_db': round(drop, 3),
            'policy': 'one_continuous_excerpt_near_energy_decay_no_loop_or_stretch',
            'cadence_note': 'Energy decay detected; musical/harmonic resolution is not certified.',
        }))
    if not candidates:
        raise VideoError('score_no_usable_continuous_excerpt')
    return max(candidates, key=lambda row: row[:3])[3]


async def _probe_audio(media: Media, source: Path) -> dict:
    raw = await media.run([
        media.settings.ffprobe_bin, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source),
    ])
    try:
        probe = json.loads(raw)
        streams = [s for s in probe['streams'] if s.get('codec_type') == 'audio']
        duration = float(probe['format']['duration'])
        if len(streams) != 1 or not math.isfinite(duration) or duration <= 0:
            raise ValueError('invalid audio')
        return {'duration_seconds': duration, 'codec': streams[0].get('codec_name'),
                'channels': streams[0].get('channels'), 'sample_rate': int(streams[0]['sample_rate'])}
    except (ValueError, KeyError, TypeError) as error:
        raise VideoError('score_audio_invalid') from error


async def _decode(media: Media, source: Path, *, start=0, duration=None) -> np.ndarray:
    args = [media.settings.ffmpeg_bin, '-v', 'error', '-nostdin', '-ss', str(start), '-i', str(source),
            '-map', '0:a:0', '-vn']
    if duration is not None:
        args.extend(['-t', str(duration)])
    args.extend(['-ac', '2', '-ar', str(SAMPLE_RATE), '-f', 'f32le', 'pipe:1'])
    raw = await media.run(args)
    samples = np.frombuffer(raw, dtype='<f4')
    if not samples.size or samples.size % 2:
        raise VideoError('score_audio_samples_invalid')
    return samples.reshape(-1, 2)


async def _excerpt(media: Media, source: Path, destination: Path, *, start, duration) -> dict:
    await media.run([
        media.settings.ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
        '-ss', str(start), '-i', str(source), '-map', '0:a:0', '-vn', '-t', str(duration),
        '-ac', '2', '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s24le', str(destination),
    ])
    info = await _probe_audio(media, destination)
    if abs(info['duration_seconds'] - duration) > 0.03:
        raise VideoError('score_excerpt_duration_invalid')
    return info


async def wait_for_score(client: ComfyUI, prompt_id: str) -> tuple[dict, float | None]:
    """Read native audio history; never route it through video-only wait()."""
    failures = 0
    while True:
        try:
            history = await client._json('GET', f'/history/{prompt_id}')
            if not isinstance(history, dict):
                raise VideoError('comfyui_history_invalid')
            asset = ace_music_asset_from_history(history, prompt_id)
            failures = 0
            if asset is not None:
                messages = history[prompt_id].get('status', {}).get('messages', [])
                return asset, client._execution_seconds(messages)
        except VideoError as error:
            if error.reason != 'comfyui_http_failed':
                raise
            failures += 1
            if failures >= 3:
                raise
        await asyncio.sleep(client.settings.poll_interval)


def score_direction(preset: str) -> tuple[str, int, str]:
    """Context/style select a positive direction, without inferring photo emotions."""
    descriptions = {
        'BRIGHT': ('Instrumental organic indie-pop for a joyful travel memory film. Warm rhythmic acoustic guitar, '
                   'playful piano, melodic bass, human-played drums and shakers. Bright, intimate and confident, '
                   'airy polished stereo sound, a clear forward groove, continuous musical development and a warm resolving ending.', 120, 'D major'),
        'WARM': ('Instrumental acoustic pop for a happy, affectionate travel memory film. Warm piano, acoustic guitar, '
                 'melodic bass and gentle live-sounding percussion. Intimate, hopeful and lightly rhythmic, '
                 'continuous musical development and a tender resolving ending with a short natural reverb tail.', 108, 'D major'),
        'CALM': ('Instrumental uplifting cinematic acoustic music for a peaceful positive travel memory film. '
                 'Delicate piano, soft acoustic guitar, warm strings and light organic percussion. Airy, hopeful and spacious, '
                 'a gentle forward pulse, continuous musical development and a calm resolving ending.', 96, 'G major'),
    }
    try:
        return descriptions[preset]
    except KeyError as error:
        raise VideoError('score_preset_invalid') from error


async def prepare_production_score(
    media: Media, client: ComfyUI | None, selection, folder: Path, *,
    duration_seconds: float, style: str = 'LIVELY', context: str = '', seed: int = 0,
    job_id: str = 'memory-film', object_info: dict | None = None,
    policy: ScorePolicy | None = None, fallback_file: Path | None = None,
    allow_preset_fallback: bool = False,
) -> tuple[Path | None, dict]:
    """Honor user choice or generate, inspect and crop a bounded ACE score.

    Caller owns Pod lifecycle and renderer volume handling. A failed/uncertain
    submission is never retried; only a completed rejected candidate or known
    provider execution failure permits the one additional attempt. Existing
    reports block duplicate paid submissions when a caller repeats the stage.
    """
    policy = policy or ScorePolicy()
    if (not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool)
            or not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= 180
            or type(seed) is not int or not 0 <= seed < 2**64):
        raise VideoError('score_request_invalid')
    working = Path(folder) / 'score'
    working.mkdir(parents=True, exist_ok=True)
    report_path = working / 'report.json'
    file_identity = None
    if selection.file is not None:
        supplied = Path(selection.file).resolve()
        stat = supplied.stat() if supplied.is_file() else None
        file_identity = {'path': str(supplied), 'size': stat.st_size if stat else None,
                         'mtime_ns': stat.st_mtime_ns if stat else None}
    identity = {
        'target_seconds': float(duration_seconds), 'preset': selection.preset,
        'volume': float(selection.volume), 'start_seconds': float(selection.start_seconds),
        'file': file_identity, 'style': style, 'context': context, 'seed': seed,
        'policy': asdict(policy), 'fallback_file': str(Path(fallback_file).resolve()) if fallback_file else None,
    }
    if allow_preset_fallback:
        identity['allow_preset_fallback'] = True
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if report_path.exists():
        old = json.loads(report_path.read_text())
        if old.get('request_fingerprint') != fingerprint:
            raise VideoError('score_existing_request_mismatch')
        if old.get('status') == 'ready' and old.get('output_file') and Path(old['output_file']).is_file():
            return Path(old['output_file']), old
        if old.get('status') == 'disabled':
            return None, old
        raise VideoError('score_existing_attempt_requires_recovery')
    started = time.monotonic()
    report = {
        'status': 'preparing', 'target_seconds': duration_seconds,
        'request_fingerprint': fingerprint,
        'requested_preset': selection.preset, 'volume': selection.volume,
        'volume_applied': False, 'attempts': [], 'fallback_used': False,
        'looping_applied': False, 'time_stretching_applied': False,
        'perceptual_quality': 'waveform_checks_do_not_certify_music_quality',
    }

    def save():
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        _save(report_path, report)

    async def prepare_file(path: Path, source_kind: str, start: float) -> tuple[Path, dict]:
        path = Path(path).resolve()
        if not path.is_file() or path.stat().st_size > 100_000_000:
            raise VideoError('music_missing_or_too_large')
        info = await _probe_audio(media, path)
        if not math.isfinite(start) or start < 0 or info['duration_seconds'] - start < duration_seconds - 0.02:
            raise VideoError('score_user_file_too_short')
        data = await _decode(media, path, start=start, duration=duration_seconds)
        activity = audio_activity(data)
        if activity['rms_dbfs'] < -50:
            raise VideoError('score_user_file_silent')
        output = working / 'score.wav'
        rendered = await _excerpt(media, path, output, start=start, duration=duration_seconds)
        report.update(status='ready', source=source_kind, source_file=str(path), output_file=str(output),
                      excerpt={'start_seconds': start, 'duration_seconds': duration_seconds},
                      audio=rendered, activity=activity)
        save()
        return output, report

    save()
    if selection.preset == 'NONE':
        report.update(status='disabled', source='NONE')
        save()
        return None, report
    if selection.file is not None:
        # User-supplied tracks are not regenerated or replaced when they fail.
        try:
            return await prepare_file(selection.file, 'USER_FILE', selection.start_seconds)
        except VideoError as error:
            report.update(status='failed', reason=error.reason)
            save()
            raise
    preset = choose_preset(selection, style, context)
    report['selected_preset'] = preset
    if client is None:
        report.update(status='failed', reason='score_client_required')
        save()
        raise VideoError('score_client_required')
    caption, bpm, keyscale = score_direction(preset)
    generated_duration = min(600, max(20, math.ceil(duration_seconds) + policy.padding_seconds))
    report.update(source='MODEL_GENERATED', model=ace_music_provenance(),
                  generated_duration_seconds=generated_duration, bpm_requested=bpm, keyscale_requested=keyscale)
    info = object_info if object_info is not None else await client.object_info()
    # Missing models/nodes fail before any paid submission, without fallbacks
    # masking a deployment configuration error.
    preflight = build_ace_music_workflow(duration=generated_duration, bpm=bpm, keyscale=keyscale,
        seed=seed, prefix=f'audigo/{job_id}/score/attempt-0', caption=caption)
    validate_ace_music_workflow(preflight, info)
    save()
    for index in range(policy.max_attempts):
        attempt_folder = working / f'attempt-{index}'
        attempt_folder.mkdir(exist_ok=False)
        entry = {'attempt': index, 'seed': (seed + index) % 2**64, 'status': 'preparing',
                 'provider_job_may_be_running': False}
        report['attempts'].append(entry)
        graph = build_ace_music_workflow(duration=generated_duration, bpm=bpm, keyscale=keyscale,
            seed=entry['seed'], prefix=f'audigo/{job_id}/score/attempt-{index}', caption=caption)
        _save(attempt_folder / 'workflow.json', graph)
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                entry.update(status='submitting', provider_job_may_be_running=True)
                save()
                prompt_id = await client.submit(graph, f'{job_id}-score-{index}')
                entry.update(status='generating', prompt_id=prompt_id)
                save()
                asset, elapsed = await wait_for_score(client, prompt_id)
                entry.update(status='downloading', provider_job_may_be_running=False,
                             provider_execution_seconds=elapsed)
                save()
                master = attempt_folder / 'master.flac'
                await client.download(asset, master)
                probe = await _probe_audio(media, master)
                if probe['duration_seconds'] > generated_duration + 2:
                    raise VideoError('score_audio_duration_invalid')
                samples = await _decode(media, master)
                entry['source_audio'] = audio_activity(samples)
                excerpt = select_score_excerpt(samples, duration_seconds, policy=policy)
                entry['excerpt'] = excerpt
                output = working / 'score.wav'
                audio = await _excerpt(media, master, output, start=excerpt['start_seconds'], duration=duration_seconds)
                entry.update(status='accepted_waveform_checks', master_file=str(master))
                report.update(status='ready', output_file=str(output), audio=audio,
                              excerpt=excerpt, accepted_attempt=index)
                save()
                return output, report
        except TimeoutError as error:
            entry.update(status='timed_out', reason='score_generation_timeout')
            report.update(status='failed', reason='score_generation_timeout')
            save()
            raise VideoError('score_generation_timeout', prompt_id=entry.get('prompt_id'),
                             provider_job_may_be_running=entry['provider_job_may_be_running']) from error
        except VideoError as error:
            entry.update(status='rejected', reason=error.reason)
            # An uncertain submit/history/download must not trigger another paid
            # composition. Only known completed failures or audio gates retry.
            retryable = (error.reason == 'ace_music_generation_failed'
                         or (not entry['provider_job_may_be_running']
                             and error.reason.startswith('score_')))
            if error.reason == 'ace_music_generation_failed':
                entry['provider_job_may_be_running'] = False
            report['last_rejection'] = error.reason
            save()
            if not retryable:
                report.update(status='failed', reason=error.reason)
                save()
                raise
    if fallback_file is not None or allow_preset_fallback:
        report['fallback_used'] = True
        try:
            if fallback_file is not None:
                return await prepare_file(fallback_file, 'CONFIGURED_FILE_FALLBACK', 0)
            fallback = working / 'preset-fallback.wav'
            try:
                await asyncio.to_thread(render_preset, fallback, preset, duration_seconds)
            except OSError as error:
                raise VideoError('score_preset_fallback_failed') from error
            return await prepare_file(fallback, 'BUILTIN_PRESET_FALLBACK', 0)
        except VideoError as error:
            report.update(status='failed', reason=error.reason)
            save()
            raise
    report.update(status='failed', reason='score_quality_failed')
    save()
    raise VideoError('score_quality_failed', attempts=len(report['attempts']))
