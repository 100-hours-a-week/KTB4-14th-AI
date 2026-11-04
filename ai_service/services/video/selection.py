"""OpenCV 지표로 불량·유사 사진을 줄이고 시간 순서를 지킨 대표 사진을 고른다."""
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from ai_service.services.video.errors import VideoError


def ordered_photo_files(folder: Path) -> list[Path]:
    """촬영 EXIF 순서로 찾는다. 시각 없는 사진은 파일명순으로 마지막에 둔다."""
    from PIL import Image
    if not folder.is_dir():
        raise VideoError('photo_folder_missing')
    rows = []
    for path in folder.rglob('*'):
        if not path.is_file() or path.name.startswith('.') or path.suffix.lower() not in {
            '.jpg', '.jpeg', '.png', '.webp', '.dng', '.heic', '.heif',
        }:
            continue
        capture = None
        try:
            with Image.open(path) as image:
                exif = image.getexif()
                nested = exif.get_ifd(34665) if 34665 in exif else {}
                value = nested.get(36867) or exif.get(36867) or exif.get(306)
                if isinstance(value, str):
                    capture = datetime.strptime(value[:19], '%Y:%m:%d %H:%M:%S')
        except (OSError, ValueError, TypeError):
            pass
        rows.append((capture, path))
    return [row[1] for row in sorted(rows, key=lambda row: (row[0] is None,
            row[0] or datetime.max, str(row[1].relative_to(folder)).casefold()))]


def quality_score(info: dict) -> float:
    quality = info['quality']
    sharpness = min(math.log1p(quality['laplacian_variance']) / math.log1p(1000), 1)
    resolution = min(info['oriented_width'] * info['oriented_height'] / (1280 * 720), 1)
    score = sharpness * 0.6 + resolution * 0.15 + (0.25 if quality['exposure'] == 'NORMAL' else 0)
    return round(score - (0.1 if quality['sharpness'] == 'SOFT' else 0), 6)


def histogram_similarity(a: dict, b: dict) -> float:
    return float(np.minimum(a['color_histogram'], b['color_histogram']).sum())


def near_duplicate(a: dict, b: dict) -> bool:
    distance = (int(a['phash'], 16) ^ int(b['phash'], 16)).bit_count()
    return distance <= 16 and histogram_similarity(a, b) >= 0.85


def select_photos(candidates: list[dict], *, max_photos=12, transition_seconds=5,
                  max_duration_seconds=180) -> dict:
    """입력은 촬영 순서다. 사진의 의미·감정을 추정하거나 순서를 새로 만들지 않는다."""
    limit = min(max_photos, max_duration_seconds // transition_seconds + 1)
    rows, eligible = [], []
    bad_quality = {'BLURRY', 'UNDEREXPOSED', 'OVEREXPOSED', 'LOW_RESOLUTION'}
    for item in candidates:
        row = {'source_index': item['source_index'], 'quality_score': quality_score(item),
               'warnings': item['quality']['warnings'], 'decision': None}
        rows.append(row)
        if bad_quality.intersection(row['warnings']):
            row['decision'] = 'quality_rejected'
        else:
            eligible.append((item, row))

    # 선명한 사진을 먼저 남긴다. 비슷한 구도·색의 사진은 더 나은 한 장으로 대표한다.
    representatives = []
    for item, row in sorted(eligible, key=lambda pair: (-pair[1]['quality_score'], pair[0]['source_index'])):
        duplicate = next((kept for kept in representatives
                          if near_duplicate(item['visual_signature'], kept['visual_signature'])), None)
        if duplicate is not None:
            row.update(decision='near_duplicate', representative_source_index=duplicate['source_index'])
        else:
            representatives.append(item)
    representatives.sort(key=lambda item: item['source_index'])
    if len(representatives) < 2:
        raise VideoError('insufficient_usable_photos', usable_count=len(representatives), required=2,
                         photo_selection={'photos': rows, 'selected_source_indices': []})

    selected = representatives
    if len(selected) > limit:
        # 여행 앞·중간·뒤가 남도록 연속 구간을 나누고 품질과 이전 선택과의 색 다양성을 함께 본다.
        selected = []
        for bin_index in range(limit):
            segment = representatives[bin_index * len(representatives) // limit:
                                      (bin_index + 1) * len(representatives) // limit]
            def rank(item):
                diversity = (1 - max(histogram_similarity(item['visual_signature'], p['visual_signature'])
                                     for p in selected)) if selected else 0
                return quality_score(item) + 0.2 * diversity, -item['source_index']
            selected.append(max(segment, key=rank))
    indices = [item['source_index'] for item in selected]
    for row in rows:
        if row['decision'] is None:
            row['decision'] = 'selected' if row['source_index'] in indices else 'representative_limit'
    duration = (len(indices) - 1) * transition_seconds
    return {'algorithm': 'opencv_quality_phash_histogram_temporal_bins_v1',
            'input_count': len(candidates), 'selected_count': len(indices),
            'selected_source_indices': indices, 'duration_seconds': duration,
            'transition_count': len(indices) - 1, 'transition_seconds': transition_seconds,
            'max_duration_seconds': max_duration_seconds, 'max_selected_photos': max_photos,
            'photos': rows,
            'limitations': 'Heuristic focus/exposure/similarity scores, not scene semantics or emotion recognition.'}
