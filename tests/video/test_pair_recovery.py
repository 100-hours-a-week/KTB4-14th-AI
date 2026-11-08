from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from ai_service.services.video.film import recovery_shot
from ai_service.services.video.diversity import MemoryFilmShot
from ai_service.services.video.errors import VideoError

def test_small_album_reallocation_does_not_relax_large_album_diversity_floor():
    from ai_service.services.video.film import fallback_minimum
    assert fallback_minimum(30,6)==3
    assert fallback_minimum(30,4)==3  # the Gangneung album: 3 of 4 scenes kept still makes a 30 s film
    assert fallback_minimum(30,15)==5
    assert fallback_minimum(30,10)==5

def test_rejected_pair_can_keep_first_reference_without_reusing_other_scenes():
    shot=MemoryFilmShot(scene_key='beach',source_index=1,end_source_index=7,role='landscape',people_visible=True,visible_scene='Waves on a sandy beach.',subject_action='Small waves wash onto shore.',camera_path='A gentle camera glide.',memory_reason='해변에서 보낸 여행의 기억',quality_score=.8,motion_potential=.7,frame_count=120,duration_seconds=5)
    recovered=recovery_shot(shot,[],120)
    assert recovered.source_indices==[1]
    assert recovered.scene_key==shot.scene_key
    assert recovered.frame_count==shot.frame_count
    assert shot.source_indices==[1,7]
    with pytest.raises(VideoError,match='no_usable_reserve'):
        recovery_shot(recovered,[],120)

def test_reserve_is_preferred_and_gets_exact_frame_budget():
    reserve=Mock();expected=object();reserve.with_frames.return_value=expected
    reserves=[reserve]
    assert recovery_shot(SimpleNamespace(source_index=1,end_source_index=7),reserves,103) is expected
    reserve.with_frames.assert_called_once_with(103,24)
    assert reserves==[]

def test_unused_photo_recovery_is_bounded_and_preserves_rejected_singletons():
    from ai_service.services.video.film import unused_photo_reserves
    from ai_service.services.video.diversity import MemorySceneCandidate
    def photo(i,motion):
        return SimpleNamespace(source_index=i,usable=True,subject_focus='CLEAR',visual_quality=.9,
            motion_potential=motion,scene_key=f'memory-{i}',category='landscape',people_visible=False,
            visible_scene='A rocky coast with visible waves.',safe_action='The waves break against the rocks.',
            memory_reason='해변의 추억을 담은 사진')
    plan=SimpleNamespace(catalog=SimpleNamespace(photos=[photo(0,.9),photo(1,.9),photo(3,.48),photo(7,.75)]))
    clips=[{'status':'accepted','source_indices':[0]},
           {'status':'rejected','source_indices':[1,7]},
           {'status':'rejected','source_indices':[1]}]
    reserves=unused_photo_reserves(plan,clips,120)
    assert [s.source_indices for s in reserves]==[[7],[3]]
    for shot in reserves:
        # Stored reserves reload as candidates; scheduling must restore frame budgets.
        candidate=MemorySceneCandidate.model_validate(shot.model_dump(exclude={'frame_count','duration_seconds'}))
        restored=candidate.with_frames(120,24)
        assert restored.duration_seconds==5
        assert restored.frame_count==120
