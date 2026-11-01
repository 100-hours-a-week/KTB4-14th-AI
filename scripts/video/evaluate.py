"""Measure an existing edit without GPU calls; never infer perceptual quality from metadata."""
from __future__ import annotations

import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import subprocess


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def measure_edit(document: dict, base_dir: Path, reviews: list[dict] | None = None) -> dict:
    """Accept a film report or a legacy take plan. Missing evidence stays unknown."""
    edit = document.get('editing', document)
    takes = edit['takes']
    fps = float(edit.get('fps', 24))
    target = document.get('target_duration_seconds', edit.get('target_seconds', edit.get('duration_seconds')))
    if not takes or not math.isfinite(fps) or fps <= 0:
        raise ValueError('nonempty takes and positive fps required')
    if target is not None and (not math.isfinite(float(target)) or float(target) <= 0):
        raise ValueError('positive target duration required')
    seen, coverage, repeated_frames = set(), Counter(), 0
    for take in takes:
        if (type(take['frame_count']) is not int or take['frame_count'] <= 0
                or type(take['start_frame']) is not int or take['start_frame'] < 0):
            raise ValueError('invalid frame range')
        key = str((base_dir / take['source']).resolve())
        if key in seen:
            repeated_frames += take['frame_count']
        seen.add(key)
        coverage[key] += take['frame_count']
    frames = sum(take['frame_count'] for take in takes)
    result = {
        'schema_version': 'memory_video_evaluation_v1',
        'edit': {
            'take_count': len(takes), 'unique_generated_sources': len(seen),
            'generated_source_reuse_count': len(takes) - len(seen),
            'generated_source_reuse_ratio': round(1 - len(seen) / len(takes), 6),
            'reused_take_frame_fraction': round(repeated_frames / frames, 6),
            'largest_source_frame_fraction': round(max(coverage.values()) / frames, 6),
            'planned_frame_count': frames, 'fps': fps, 'planned_seconds': frames / fps,
            'target_seconds': target,
            'planned_duration_error_frames': abs(frames - target * fps) if target is not None else None,
            'planned_scene_count': document.get('planned_scene_count'),
            'final_reference_photo_count': len(document['final_source_indices']) if 'final_source_indices' in document else None,
            'native_source_sizes': edit.get('native_source_sizes'),
        },
        'sampled_reviews': None,
        'decoded_media': None,
        'perceptual_quality_certified': False,
        'limitations': [
            'Source reuse compares file paths, not perceptual similarity or semantic scene identity.',
            'Review metrics are sampled heuristic/model decisions, not human defect rates or calibrated probabilities.',
            'Missing measurements are null. Edit frame counts alone do not prove successful video decoding.',
            'Job report elapsed time is not assumed to include queueing, all failed attempts, or final fallback review.',
        ],
    }
    if reviews is not None:
        indexed = {}
        for row in reviews:
            review = row['review']
            key = (row['scene_key'], review.get('start_frame'), row['frame_count'])
            if key in indexed:
                raise ValueError('duplicate review window')
            indexed[key] = row
        matched = [indexed.get((t.get('memory_role'), t['start_frame'], t['frame_count'])) for t in takes]
        rows = [row for row in matched if row is not None]
        technical = [row['review'].get('measurements', {}) for row in rows]
        people = [row['people_fidelity'] for row in rows if 'people_fidelity' in row]
        def maximum(field):
            values = [row[field] for row in technical if field in row]
            return max(values) if values else None
        result['sampled_reviews'] = {
            'matched_take_count': len(rows), 'missing_take_count': len(takes) - len(rows),
            'technical_and_semantic_accepted_count': sum(row['review'].get('accepted') is True and
                row['review'].get('semantic', {}).get('accepted') is True for row in rows),
            'people_reviewed_take_count': len(people),
            'people_accepted_take_count': sum(row.get('accepted') is True for row in people),
            'unmatched_foreground_people_flags': sum(len(row['unmatched_foreground_people']) for row in people) if people else None,
            'uncertain_foreground_people_flags': sum(len(row['uncertain_foreground_people']) for row in people) if people else None,
            'max_take_frozen_fraction': maximum('frozen_fraction'),
            'max_take_frozen_run_seconds': maximum('longest_frozen_seconds'),
            'max_take_blur_fraction': maximum('blur_fraction'),
        }
    return result


def probe_video(path: Path, *, ffprobe: str = 'ffprobe') -> dict:
    raw = subprocess.run([
        ffprobe, '-v', 'error', '-count_frames', '-show_entries',
        'stream=codec_type,width,height,avg_frame_rate,nb_read_frames,sample_rate,channels:format=duration',
        '-of', 'json', str(path),
    ], check=True, capture_output=True, timeout=180)
    data = json.loads(raw.stdout)
    videos = [s for s in data['streams'] if s['codec_type'] == 'video']
    audio = [s for s in data['streams'] if s['codec_type'] == 'audio']
    if len(videos) != 1:
        raise ValueError('exactly one video stream required')
    stream = videos[0]
    fps = float(Fraction(stream['avg_frame_rate']))
    frames = int(stream['nb_read_frames'])
    return {
        'sha256': sha256(path), 'decoded_frame_count': frames, 'fps': fps,
        'decoded_duration_seconds': frames / fps,
        'container_duration_seconds': float(data['format']['duration']),
        'export_dimensions': [stream['width'], stream['height']],
        'audio_stream_count': len(audio),
        'audio_channels': audio[0].get('channels') if audio else None,
        'audio_sample_rate': int(audio[0]['sample_rate']) if audio else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True, help='report.json or legacy take-plan.json')
    parser.add_argument('--reviews', type=Path, help='final-fidelity-reviews.json for the exact final windows')
    parser.add_argument('--video', type=Path, help='decode an actual delivered video using ffprobe')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = measure_edit(json.loads(args.report.read_text()), args.report.parent,
                          json.loads(args.reviews.read_text()) if args.reviews else None)
    result['evidence_sha256'] = {'report': sha256(args.report), 'reviews': sha256(args.reviews) if args.reviews else None}
    if args.video:
        result['decoded_media'] = probe_video(args.video)
        target = result['edit']['target_seconds']
        if target is not None:
            result['decoded_media']['duration_error_frames'] = abs(
                result['decoded_media']['decoded_frame_count'] - target * result['decoded_media']['fps'])
    output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
