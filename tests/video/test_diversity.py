"""Service diversity invariants on unrelated synthetic collections, not Leon IDs."""
import unittest

from ai_service.services.video.diversity import (
    MemoryCatalog, MemoryPhotoAnalysis, MemorySceneGroup, MemorySceneGrouping,
    allocate_scene_frames, build_memory_film_plan, scene_candidates_from_catalog,
    target_scene_count, validate_catalog,
)
from ai_service.services.video.errors import VideoError


def photo(index, category='landscape', **updates):
    values = dict(source_index=index, scene_key=f'place-{index}', category=category,
                  visible_scene=f'A distinct outdoor travel place number {index} with a clear foreground.',
                  subject_description='The main place and foreground.', subject_focus='CLEAR',
                  visual_quality=.85, motion_potential=.8, people_visible=False,
                  safe_action='Nearby foliage sways naturally while the water moves.',
                  camera_path='Move physically past the foreground with clear depth parallax.',
                  memory_reason='그 장소에서 보았던 풍경과 이동의 순간을 남긴다.',
                  usable=True, rejection_reason=None)
    values.update(updates)
    return MemoryPhotoAnalysis(**values)


def catalog(photos, groups=None):
    if groups is None:
        groups = [MemorySceneGroup(scene_key=f'memory-{p.source_index}',
                                  source_indices=[p.source_index], first_last_pair=None)
                  for p in photos if p.usable and p.subject_focus != 'BLURRY']
    return MemoryCatalog(input_signature='a' * 64, photos=photos, model='test-vision',
                         grouping=MemorySceneGrouping(
                             title='좋았던 여행의 순간', closing_line='다시 떠올리는 하루',
                             narrative='사진에 남은 서로 다른 여행의 순간을 시간 순서로 경험한다.',
                             emotional_arc='출발의 기대에서 여행의 경험과 따뜻한 여운으로 이어진다.',
                             music_direction='One warm positive instrumental with a clear rhythmic build and resolved ending.',
                             groups=groups))


class DiversityTests(unittest.TestCase):
    def assert_non_repeating_exact_film(self, plan, expected_count):
        self.assertEqual(len(plan.shots), expected_count)
        self.assertEqual(sum(s.frame_count for s in plan.shots), 720)
        self.assertEqual(plan.frame_count, 720)
        self.assertEqual(plan.fps, 24)
        self.assertEqual(len({s.scene_key for s in plan.shots}), expected_count)
        sources = [i for s in plan.shots for i in s.source_indices]
        self.assertEqual(len(sources), len(set(sources)))
        self.assertEqual(plan.diversity['scene_repeat_count'], 0)
        self.assertEqual(plan.diversity['source_repeat_count'], 0)
        self.assertTrue(all(s.duration_seconds == s.frame_count / 24 for s in plan.shots))

    def test_five_photos_adapt_without_looping_to_fifteen_scenes(self):
        p = build_memory_film_plan(catalog([photo(i) for i in range(5)]))
        self.assert_non_repeating_exact_film(p, 5)
        self.assertTrue(p.diversity['limited_variety'])
        self.assertEqual([s.source_indices for s in p.shots], [[i] for i in range(5)])
        self.assertTrue(all(s.source_index == s.end_source_index for s in p.shots))

    def test_ten_distinct_memories_use_all_ten_not_six(self):
        p = build_memory_film_plan(catalog([photo(i, ['food', 'transport', 'landscape'][i % 3]) for i in range(10)]))
        self.assert_non_repeating_exact_film(p, 10)
        self.assertEqual(p.diversity['selected_source_indices'], list(range(10)))
        self.assertGreater(len(set(s.frame_count for s in p.shots)), 1)

    def test_hundred_photos_cover_collection_with_fifteen_unique_scenes_and_reserves(self):
        p = build_memory_film_plan(catalog([photo(i, ['food', 'transport', 'landscape', 'night'][i % 4]) for i in range(100)]))
        self.assert_non_repeating_exact_film(p, 15)
        self.assertEqual(len(p.reserves), 85)
        self.assertTrue(all(s.frame_count >= 48 for s in p.shots))
        chosen = p.diversity['selected_source_indices']
        self.assertLess(min(chosen), 10)
        self.assertGreater(max(chosen), 90)
        self.assertGreaterEqual(len(p.diversity['category_counts']), 4)

    def test_hundred_duplicate_views_are_one_scene_and_do_not_fake_variety(self):
        photos = [photo(i) for i in range(100)]
        groups = [MemorySceneGroup(scene_key='one-actual-coastal-overlook',
                                  source_indices=list(range(100)), first_last_pair=[0, 1])]
        p = build_memory_film_plan(catalog(photos, groups))
        self.assert_non_repeating_exact_film(p, 1)
        self.assertEqual(p.diversity['available_unique_scenes'], 1)
        self.assertTrue(p.diversity['limited_variety'])

    def test_blurred_subject_is_excluded_even_with_high_global_scene_quality(self):
        photos = [photo(0, 'food', subject_focus='BLURRY', visual_quality=1,
                        subject_description='Blurred snack before a detailed cafe.'), photo(1), photo(2)]
        p = build_memory_film_plan(catalog(photos))
        self.assert_non_repeating_exact_film(p, 2)
        self.assertNotIn(0, p.diversity['selected_source_indices'])

    def test_semantic_group_cannot_share_a_photo_with_another_group(self):
        c = catalog([photo(i) for i in range(3)], [
            MemorySceneGroup(scene_key='same-place', source_indices=[0, 1], first_last_pair=[0, 1]),
            MemorySceneGroup(scene_key='another-place', source_indices=[1, 2], first_last_pair=[1, 2]),
        ])
        with self.assertRaises(VideoError):
            validate_catalog(c, [0, 1, 2])

    def test_far_apart_pair_does_not_force_an_unproven_continuous_visit(self):
        c = catalog([photo(2), photo(100)], [MemorySceneGroup(
            scene_key='return-to-the-same-place', source_indices=[2, 100], first_last_pair=[2, 100])])
        scene = scene_candidates_from_catalog(c)[0]
        self.assertEqual(scene.source_index, scene.end_source_index)
        self.assertEqual(len(scene.source_indices), 1)

    def test_reserve_replacement_keeps_timing_and_never_reuses_a_selected_scene(self):
        p = build_memory_film_plan(catalog([photo(i) for i in range(20)]))
        failed = p.shots[3]
        replacement = p.reserves[0].with_frames(failed.frame_count, p.fps)
        self.assertEqual(replacement.frame_count, failed.frame_count)
        self.assertEqual(replacement.duration_seconds, failed.duration_seconds)
        self.assertNotIn(replacement.scene_key, [s.scene_key for s in p.shots])
        self.assertFalse(set(replacement.source_indices) & {i for s in p.shots for i in s.source_indices})

    def test_duration_and_count_policy_rejects_impossible_or_invalid_settings(self):
        for count, duration in [(0, 30), (1, float('nan')), (2, -1)]:
            with self.assertRaises(ValueError):
                target_scene_count(count, duration)
        with self.assertRaises(ValueError):
            allocate_scene_frames([scene_candidates_from_catalog(catalog([photo(0)]))[0]], .01)
        self.assertEqual(target_scene_count(100, 30), 15)
        self.assertEqual(target_scene_count(100, 30, max_scenes=12), 12)

    def test_people_flag_uses_selected_references_not_unselected_members(self):
        c = catalog([photo(0), photo(1), photo(2, people_visible=True)], [
            MemorySceneGroup(scene_key='one-view-with-distant-passerby', source_indices=[0, 1, 2], first_last_pair=[0, 1])])
        self.assertFalse(scene_candidates_from_catalog(c)[0].people_visible)


class CalmAlbumTests(unittest.TestCase):
    def test_album_of_only_calm_photos_still_makes_a_film(self):
        # 2026-11-09: ten restaurant photos (plated food, motion potential below 0.5) failed the whole job.
        photos = [photo(i, 'food', motion_potential=.3 + i * .01) for i in range(10)]
        scenes = scene_candidates_from_catalog(catalog(photos))
        self.assertEqual(len(scenes), 4)
        plan = build_memory_film_plan(catalog(photos), duration=30)
        self.assertEqual(sum(s.frame_count for s in plan.shots), 720)

    def test_calm_photos_only_fill_up_a_thin_album(self):
        lively = [photo(i) for i in range(2)]
        calm = [photo(i, 'food', motion_potential=.3, visual_quality=.5 + i * .01) for i in range(2, 8)]
        scenes = scene_candidates_from_catalog(catalog(lively + calm))
        self.assertEqual(len(scenes), 4)
        self.assertEqual({0, 1} <= {s.source_index for s in scenes}, True)
        self.assertEqual({s.source_index for s in scenes} - {0, 1}, {6, 7})  # best-looking calm shots

    def test_enough_lively_photos_leave_calm_ones_out(self):
        photos = [photo(i) for i in range(5)] + [photo(i, 'food', motion_potential=.3) for i in range(5, 8)]
        self.assertEqual({s.source_index for s in scene_candidates_from_catalog(catalog(photos))}, set(range(5)))
