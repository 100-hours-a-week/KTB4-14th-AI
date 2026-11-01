"""Regression checks for honest comparison: repeated cuts, missing and stale evidence."""
from pathlib import Path
import unittest

from scripts.video.evaluate import measure_edit


class EvaluationTests(unittest.TestCase):
    def test_repeated_source_counts_include_duration_not_just_cut_count(self):
        report = {'fps': 24, 'target_seconds': 4, 'takes': [
            {'source': 'a.mp4', 'start_frame': 0, 'frame_count': 24},
            {'source': 'b.mp4', 'start_frame': 0, 'frame_count': 24},
            {'source': './a.mp4', 'start_frame': 24, 'frame_count': 48},
        ]}
        measured = measure_edit(report, Path('/tmp'))
        self.assertEqual(measured['edit']['unique_generated_sources'], 2)
        self.assertEqual(measured['edit']['generated_source_reuse_count'], 1)
        self.assertEqual(measured['edit']['reused_take_frame_fraction'], .5)
        self.assertEqual(measured['edit']['largest_source_frame_fraction'], .75)
        self.assertIsNone(measured['sampled_reviews'])
        self.assertIsNone(measured['decoded_media'])

    def test_review_of_old_window_cannot_certify_resized_final_window(self):
        report = {'editing': {'takes': [
            {'source': 'a.mp4', 'memory_role': 'beach', 'start_frame': 12, 'frame_count': 72},
        ]}}
        review = {'scene_key': 'beach', 'frame_count': 48, 'review': {'start_frame': 12, 'accepted': True}}
        measured = measure_edit(report, Path('/tmp'), [review])['sampled_reviews']
        self.assertEqual(measured['missing_take_count'], 1)
        self.assertEqual(measured['people_reviewed_take_count'], 0)
        self.assertIsNone(measured['unmatched_foreground_people_flags'])
        self.assertIsNone(measured['max_take_frozen_fraction'])

    def test_matching_rejection_is_counted_without_dropping_flags(self):
        report = {'takes': [
            {'source': 'a.mp4', 'memory_role': 'beach', 'start_frame': 0, 'frame_count': 72},
        ]}
        review = {'scene_key': 'beach', 'frame_count': 72,
                  'review': {'start_frame': 0, 'accepted': True, 'semantic': {'accepted': True},
                             'measurements': {'frozen_fraction': .2, 'longest_frozen_seconds': .5}},
                  'people_fidelity': {'accepted': False, 'unmatched_foreground_people': ['invented person'],
                                      'uncertain_foreground_people': []}}
        measured = measure_edit(report, Path('/tmp'), [review])['sampled_reviews']
        self.assertEqual(measured['technical_and_semantic_accepted_count'], 1)
        self.assertEqual(measured['people_accepted_take_count'], 0)
        self.assertEqual(measured['unmatched_foreground_people_flags'], 1)
        self.assertEqual(measured['max_take_frozen_run_seconds'], .5)

    def test_duplicate_review_windows_are_ambiguous(self):
        report = {'takes': [{'source': 'a.mp4', 'start_frame': 0, 'frame_count': 24}]}
        review = {'scene_key': 'beach', 'frame_count': 24, 'review': {'start_frame': 0}}
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            measure_edit(report, Path('/tmp'), [review, review])
