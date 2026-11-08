import asyncio
import json
from types import SimpleNamespace

import pytest

from ai_service.services.video import diversity, film, quality
from ai_service.services.video.errors import VideoError


def setup(tmp_path, monkeypatch, approved, planned, verdict):
    """approved scene count, planned scene count, verdict(scene_key, frames) -> accepted?"""
    shots = [SimpleNamespace(scene_key=f's{i}', visible_scene='v', subject_action='a') for i in range(planned)]
    plan = SimpleNamespace(shots=shots, reserves=[], title='t', closing_line='c')
    monkeypatch.setattr(diversity.MemoryFilmPlan, 'model_validate_json', staticmethod(lambda text: plan))
    clips = [{'scene_key': f's{i}', 'source_indices': [i], 'status': 'accepted', 'attempt': 0, 'slot': i}
             for i in range(approved)]
    (tmp_path / 'report.json').write_text(json.dumps({'clips': clips}))
    (tmp_path / 'plan.json').write_text('{}')
    calls = []

    async def review(http, settings, source, refs, *, duration_seconds, visible_scene, subject_action):
        key = f's{int(source.parent.name.split("-")[1])}'
        frames = round(duration_seconds * 24)
        calls.append((key, frames))
        return {'accepted': verdict(key, frames), 'start_frame': 10, 'reasons': []}

    async def fidelity(*args, **kwargs):
        return {'accepted': True}

    async def score(*args, **kwargs):
        return 'score.wav', {'source': 'MODEL_GENERATED'}

    rendered = {}

    async def render(media, takes, folder, score, **kwargs):
        rendered['takes'] = takes
        return {'output_file': 'final.mp4', 'media': {'audio_streams': 1}}

    monkeypatch.setattr(film, 'select_reviewed_take', review)
    monkeypatch.setattr(quality, 'review_people_fidelity', fidelity)
    monkeypatch.setattr(film, 'prepare_production_score', score)
    monkeypatch.setattr(film, 'render_premium_film', render)
    monkeypatch.setattr(film, 'Media', lambda settings: None)
    request = SimpleNamespace(duration_seconds=30, music=SimpleNamespace(volume=.65), memory_style='WARM',
                              memory_context='', seed=1)
    return calls, rendered, lambda: asyncio.run(film.finalize_accepted_scenes(None, SimpleNamespace(music_preset_fallback=True), request, tmp_path))


def test_reviewer_flip_after_a_drop_no_longer_fails_the_film(tmp_path, monkeypatch):
    # 9 approved of 13 planned (need 5). One scene fails at the first length; then a scene that passed
    # before fails at the longer length — the case that failed the 2026-11-06 Gangneung run.
    def verdict(key, frames):
        return not (key == 's2' or (key == 's0' and frames > 100))
    calls, rendered, run = setup(tmp_path, monkeypatch, approved=9, planned=13, verdict=verdict)
    report = run()
    assert report['status'] == 'completed'
    takes = rendered['takes']
    assert len(takes) == 7 and sum(t.frame_count for t in takes) == 720
    assert max(t.frame_count for t in takes) <= 144  # never longer than the window each scene passed
    assert len({frames for _, frames in calls}) == 2  # exactly two review lengths: at most two rounds


def test_all_scenes_passing_takes_a_single_round(tmp_path, monkeypatch):
    calls, rendered, run = setup(tmp_path, monkeypatch, approved=6, planned=10, verdict=lambda k, f: True)
    assert run()['status'] == 'completed'
    assert len(calls) == 6 and len(rendered['takes']) == 6


def test_too_few_surviving_scenes_still_fails_clearly(tmp_path, monkeypatch):
    calls, rendered, run = setup(tmp_path, monkeypatch, approved=6, planned=10, verdict=lambda k, f: k in ('s0', 's1'))
    with pytest.raises(VideoError, match='insufficient_approved_scenes'):
        run()
