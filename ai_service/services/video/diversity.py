"""사진 묶음의 의미·피사체 상태를 기록하고 반복 없는 정확한 길이의 편집안을 만든다.

Vision scores are uncalibrated screening judgements. They are not identity,
nationality or emotion classifiers and do not certify generated-video quality.
"""
from __future__ import annotations

from collections import Counter
import math
from typing import Literal

from pydantic import Field

from ai_service.schemas.common import StrictModel
from ai_service.services.video.errors import VideoError

SceneCategory = Literal['arrival', 'transport', 'landscape', 'activity', 'people',
                        'food', 'detail', 'night', 'departure', 'other']


class MemoryPhotoAnalysis(StrictModel):
    source_index: int = Field(ge=0, strict=True)
    scene_key: str = Field(min_length=3, max_length=80)
    category: SceneCategory
    visible_scene: str = Field(min_length=10, max_length=240)
    subject_description: str = Field(min_length=3, max_length=100)
    subject_focus: Literal['CLEAR', 'SOFT', 'BLURRY', 'UNKNOWN']
    visual_quality: float = Field(ge=0, le=1)
    motion_potential: float = Field(ge=0, le=1)
    people_visible: bool
    safe_action: str = Field(min_length=10, max_length=240)
    camera_path: str = Field(min_length=10, max_length=240)
    memory_reason: str = Field(min_length=5, max_length=160)
    usable: bool
    rejection_reason: str | None


class MemoryPhotoBatch(StrictModel):
    photos: list[MemoryPhotoAnalysis] = Field(min_length=1, max_length=24)


class MemorySceneGroup(StrictModel):
    scene_key: str = Field(min_length=3, max_length=80)
    source_indices: list[int] = Field(min_length=1)
    first_last_pair: list[int] | None


class MemorySceneGrouping(StrictModel):
    title: str = Field(min_length=1, max_length=40)
    closing_line: str = Field(min_length=1, max_length=60)
    narrative: str = Field(min_length=20, max_length=800)
    emotional_arc: str = Field(min_length=10, max_length=400)
    music_direction: str = Field(min_length=10, max_length=400)
    groups: list[MemorySceneGroup] = Field(min_length=1, max_length=128)


class MemoryCatalog(StrictModel):
    input_signature: str = Field(min_length=64, max_length=64)
    photos: list[MemoryPhotoAnalysis] = Field(min_length=1)
    grouping: MemorySceneGrouping
    model: str


class MemorySceneCandidate(StrictModel):
    scene_key: str
    source_index: int
    end_source_index: int
    role: SceneCategory
    people_visible: bool
    visible_scene: str
    subject_action: str
    camera_path: str
    memory_reason: str
    quality_score: float
    motion_potential: float

    @property
    def source_indices(self) -> list[int]:
        return sorted({self.source_index, self.end_source_index})

    def with_frames(self, frame_count: int, fps: int = 24) -> 'MemoryFilmShot':
        return MemoryFilmShot(**self.model_dump(), frame_count=frame_count,
                              duration_seconds=frame_count / fps)


class MemoryFilmShot(MemorySceneCandidate):
    frame_count: int = Field(gt=0, strict=True)
    duration_seconds: float = Field(gt=0)


class MemoryFilmPlan(StrictModel):
    title: str
    closing_line: str
    narrative: str
    emotional_arc: str
    music_direction: str
    fps: int = Field(default=24, strict=True)
    duration_seconds: float
    frame_count: int
    shots: list[MemoryFilmShot] = Field(min_length=1)
    reserves: list[MemorySceneCandidate]
    catalog: MemoryCatalog
    diversity: dict


def usable_photo(photo: MemoryPhotoAnalysis) -> bool:
    """The principal subject overrides a sharp but irrelevant background."""
    return (photo.usable and photo.subject_focus != 'BLURRY'
            and photo.visual_quality >= 0.35 and photo.motion_potential >= 0.2)


def photo_score(photo: MemoryPhotoAnalysis) -> float:
    focus_penalty = {'CLEAR': 0, 'SOFT': 0.12, 'UNKNOWN': 0.18, 'BLURRY': 1}[photo.subject_focus]
    return round(0.6 * photo.visual_quality + 0.4 * photo.motion_potential - focus_penalty, 6)


def validate_catalog(catalog: MemoryCatalog, candidate_indices: list[int]) -> None:
    expected = set(candidate_indices)
    actual = [photo.source_index for photo in catalog.photos]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise VideoError('memory_catalog_sources_invalid')
    eligible = {photo.source_index for photo in catalog.photos if usable_photo(photo)}
    members = [i for group in catalog.grouping.groups for i in group.source_indices]
    keys = [group.scene_key for group in catalog.grouping.groups]
    if (len(members) != len(set(members)) or set(members) != eligible
            or len(keys) != len(set(keys))):
        raise VideoError('memory_scene_grouping_invalid')
    for group in catalog.grouping.groups:
        if group.source_indices != sorted(set(group.source_indices)):
            raise VideoError('memory_scene_grouping_invalid')
        pair = group.first_last_pair
        if pair is not None and (len(pair) != 2 or pair[0] >= pair[1]
                                  or not set(pair) <= set(group.source_indices)):
            raise VideoError('memory_scene_pair_invalid')


def scene_candidates_from_catalog(catalog: MemoryCatalog) -> list[MemorySceneCandidate]:
    """One candidate per distinct memory; an uncertain pair becomes one-photo FLF."""
    lookup = {photo.source_index: photo for photo in catalog.photos}
    scenes = []
    for group in catalog.grouping.groups:
        photos = [lookup[index] for index in group.source_indices if usable_photo(lookup[index])]
        if not photos:
            continue
        best = max(photos, key=lambda photo: (photo_score(photo), -photo.source_index))
        first, last = best.source_index, best.source_index
        pair = group.first_last_pair
        # A shared subject does not establish a physically continuous viewpoint
        # across a distant part of a trip. Prefer an honest repeated endpoint.
        if pair and pair[1] - pair[0] <= 8 and all(usable_photo(lookup[i]) for i in pair):
            first, last = pair
            best = lookup[first]
        if best.motion_potential < 0.5:
            continue
        scenes.append(MemorySceneCandidate(
            scene_key=group.scene_key, source_index=first, end_source_index=last,
            role=best.category, people_visible=any(lookup[i].people_visible for i in {first, last}),
            visible_scene=best.visible_scene, subject_action=best.safe_action,
            camera_path=best.camera_path, memory_reason=best.memory_reason,
            quality_score=round(sum(photo_score(lookup[i]) for i in {first, last}) / len({first, last}), 6),
            motion_potential=best.motion_potential,
        ))
    return sorted(scenes, key=lambda scene: (scene.source_index, scene.scene_key))


def target_scene_count(available: int, duration: float, *, max_scenes: int | None = None,
                       min_scene_seconds: float = 2.0) -> int:
    if (type(available) is not int or available < 1 or not math.isfinite(duration) or duration <= 0
            or not math.isfinite(min_scene_seconds) or min_scene_seconds <= 0
            or (max_scenes is not None and (type(max_scenes) is not int or max_scenes < 1))):
        raise ValueError('Invalid adaptive scene policy')
    duration_capacity = max(1, math.floor(duration / min_scene_seconds))
    capacity = min(available, duration_capacity, max_scenes if max_scenes is not None else available)
    if max_scenes is None and 10 < available <= duration_capacity:
        capacity = max(10, capacity - 2)
    return capacity


def select_distinct_scenes(scenes: list[MemorySceneCandidate], count: int) -> tuple[list[MemorySceneCandidate], list[MemorySceneCandidate]]:
    """Greedy coverage over memory units, categories and the whole input order."""
    if not scenes or not 1 <= count <= len(scenes):
        raise ValueError('Invalid scene selection count')
    selected = []
    remaining = list(scenes)
    categories = Counter()
    span = max(1, max(s.source_index for s in scenes) - min(s.source_index for s in scenes))
    while remaining and len(selected) < count:
        def score(scene):
            novelty = 0.26 / (1 + categories[scene.role])
            coverage = (min(abs(scene.source_index - previous.source_index) for previous in selected) / span
                        if selected else 0.0)
            # Strong personal actions are useful, but do not force people or food
            # into a collection which contains only landscapes.
            active = 0.04 if scene.role in {'activity', 'people', 'transport'} else 0
            return scene.quality_score + novelty + 0.2 * coverage + active, -scene.source_index
        winner = max(remaining, key=score)
        selected.append(winner)
        categories[winner.role] += 1
        remaining.remove(winner)
    selected.sort(key=lambda scene: (scene.source_index, scene.scene_key))
    remaining.sort(key=lambda scene: (-scene.quality_score, scene.source_index))
    return selected, remaining


def allocate_scene_frames(scenes: list[MemorySceneCandidate], duration: float, fps: int = 24) -> list[int]:
    """Exact integer frames; pace varies while the total length never drifts."""
    if type(fps) is not int or fps <= 0 or not scenes or not math.isfinite(duration):
        raise ValueError('Invalid film timing')
    total = round(duration * fps)
    if abs(total - duration * fps) > 1e-6 or total < len(scenes):
        raise ValueError('Film duration must map to enough exact frames')
    base = min(round(2 * fps), total // len(scenes))
    role_weights = {'arrival': .85, 'departure': .9, 'detail': .85, 'food': .95,
                    'transport': 1.15, 'activity': 1.15, 'people': 1.1,
                    'landscape': 1.05, 'night': 1.0, 'other': 1.0}
    weights = [role_weights[s.role] * (.8 + .4 * s.motion_potential) for s in scenes]
    remaining = total - base * len(scenes)
    exact = [remaining * weight / sum(weights) for weight in weights]
    extra = [math.floor(value) for value in exact]
    for index in sorted(range(len(scenes)), key=lambda i: (-(exact[i] - extra[i]), i))[:remaining - sum(extra)]:
        extra[index] += 1
    return [base + value for value in extra]


def build_memory_film_plan(catalog: MemoryCatalog, *, duration: float = 30,
                           max_scenes: int | None = None, fps: int = 24) -> MemoryFilmPlan:
    validate_catalog(catalog, [photo.source_index for photo in catalog.photos])
    candidates = scene_candidates_from_catalog(catalog)
    if not candidates:
        raise VideoError('insufficient_usable_memory_scenes')
    count = target_scene_count(len(candidates), duration, max_scenes=max_scenes)
    selected, reserves = select_distinct_scenes(candidates, count)
    frame_counts = allocate_scene_frames(selected, duration, fps)
    shots = [scene.with_frames(frames, fps) for scene, frames in zip(selected, frame_counts)]
    source_indices = [i for shot in shots for i in shot.source_indices]
    scene_keys = [shot.scene_key for shot in shots]
    if len(source_indices) != len(set(source_indices)) or len(scene_keys) != len(set(scene_keys)):
        raise VideoError('memory_film_repeated_scene')
    grouping = catalog.grouping
    limited = len(candidates) < math.floor(duration / 2)
    return MemoryFilmPlan(
        title=grouping.title, closing_line=grouping.closing_line, narrative=grouping.narrative,
        emotional_arc=grouping.emotional_arc, music_direction=grouping.music_direction,
        fps=fps, duration_seconds=duration, frame_count=sum(frame_counts), shots=shots,
        reserves=reserves, catalog=catalog,
        diversity={'policy': 'unique_memory_units_adaptive_duration_v1',
                   'available_unique_scenes': len(candidates), 'selected_scene_count': len(shots),
                   'reserve_scene_count': len(reserves), 'scene_repeat_count': 0, 'source_repeat_count': 0,
                   'selected_scene_keys': scene_keys, 'selected_source_indices': sorted(source_indices),
                   'category_counts': dict(Counter(shot.role for shot in shots)),
                   'limited_variety': limited,
                   'availability_note': ('Available distinct usable memories limit the scene count; no source is looped to fill time.'
                                         if limited else 'Scene count is set by available unique memories and film pacing, not a six-shot reference cap.'),
                   'screening_note': 'Vision focus/motion and OpenCV scores are screening heuristics, not a guarantee of generation quality.'},
    )
