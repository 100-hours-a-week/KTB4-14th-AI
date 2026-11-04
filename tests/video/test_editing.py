import unittest
import numpy as np
from ai_service.services.video.editing import find_overlap_ranges


class EditingTests(unittest.TestCase):
    def setUp(self):
        rng=np.random.default_rng(7)
        self.first=rng.integers(0,256,(54,96,3),dtype=np.uint8)
        self.last=np.flip(self.first,axis=1).copy()

    def test_only_blend_is_cut_and_both_endpoint_guides_survive(self):
        blend=[(a*self.first+(1-a)*self.last).astype(np.uint8) for a in np.linspace(1,0,20)]
        frames=np.stack([self.first]*40+blend+[self.last]*40)
        result=find_overlap_ranges(frames,[self.first,self.last],[0,99])
        self.assertTrue(result['applied'])
        self.assertGreater(result['removed_frames'],10)
        self.assertLess(result['removed_frames'],35)
        self.assertEqual(result['retained_keyframe_indices'],[0,99-result['removed_frames']])

    def test_motion_is_kept_and_excessive_blending_is_sent_for_review(self):
        moving=np.stack([np.roll(self.first,i,axis=1) for i in range(100)])
        self.assertFalse(find_overlap_ranges(moving,[self.first,self.last],[0,99])['applied'])
        blend=np.stack([(a*self.first+(1-a)*self.last).astype(np.uint8) for a in np.linspace(1,0,100)])
        result=find_overlap_ranges(blend,[self.first,self.last],[0,99])
        self.assertFalse(result['applied'])
        self.assertEqual(result['status'],'review_needed')
