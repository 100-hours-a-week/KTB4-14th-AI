"""Synthetic data reproducibility and the actual ranking policy's boundaries."""
from collections import Counter
import json
from pathlib import Path
import unittest


from ai_service.matching.backend import BACKEND_MATCHING_EXAMPLE, BackendMatchingRequest
from ai_service.matching.service import rank_candidates
from ai_service.matching.dev import DATA_DIR, eligible_profile, make_dataset
from scripts.matching.evaluate_matching_dev import metrics


class MatchingDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.fingerprint = make_dataset()

    def test_catalog_exactly_matches_supplied_backend_enums_and_region_seed(self):
        import zipfile, re
        from ai_service.matching.catalog import CATALOG, THEMES, PACES, GENDERS, REGIONS
        self.assertEqual(THEMES,('NATURE','FOOD','CULTURE','REST','SNS','ACTIVITY'))
        self.assertEqual(PACES,('RELAXED','BALANCED','PACKED'))
        self.assertEqual(GENDERS,('FEMALE','MALE'))
        self.assertEqual(len(REGIONS),245)
        archive=Path('/Users/samrobert/Downloads/KTB4-14th-BE-development.zip')
        if archive.is_file():
            import hashlib
            with zipfile.ZipFile(archive) as zipped:
                for key,source in CATALOG['sources'].items():
                    content=zipped.read('KTB4-14th-BE-development/'+source['path'])
                    self.assertEqual(hashlib.sha256(content).hexdigest(),source['sha256'])
                    text=content.decode()
                    if key=='regions':
                        parsed=[dict(name=a,full_name=b) for a,b in re.findall(r"\('([^']+)',\s*'([^']+)',\s*NOW\(\),\s*NOW\(\)\)",text)]
                    else:
                        parsed=re.findall(r'\b[A-Z][A-Z_]+\b',text.split('{',1)[1].split('}',1)[0])
                    self.assertEqual(parsed,CATALOG[key])

    def test_reproducible_data_uses_only_backend_profile_and_request_fields(self):
        from ai_service.matching.catalog import THEMES, PACES, GENDERS, PREFERRED_GENDERS
        repeated,fingerprint=make_dataset()
        self.assertEqual(fingerprint,self.fingerprint)
        self.assertEqual(repeated,self.data)
        self.assertEqual(len(self.data['profiles']),1000)
        self.assertEqual(len({r['requester_id'] for r in self.data['requests']}),1000)
        for p in self.data['profiles']:
            self.assertLessEqual(set(p['themes']),set(THEMES))
            self.assertLessEqual(len(p['themes']),4)
            self.assertIn(p['pace'],PACES)
            self.assertIn(p['gender'],(*GENDERS,None))
            self.assertFalse({'trip','introduction','budget_min','budget_max'} & set(p))
        for r in self.data['requests']:
            self.assertLessEqual(set(r['themes']),set(THEMES))
            self.assertTrue(1<=len(r['themes'])<=3)
            self.assertIn(r['gender'],PREFERRED_GENDERS)
            self.assertNotIn('travel_plan_id',r)
            self.assertEqual(r['budget_min'] is None,r['budget_max'] is None)
            if r['budget_min'] is not None:
                self.assertTrue(0<=r['budget_min']<=r['budget_max']<=3000000)

    def test_requester_holdout_and_label_membership(self):
        splits={rid:split for rid,split,*_ in self.data['cases']}
        self.assertEqual(Counter(splits.values()),{'DEV':600,'TEST':300,'EDGE':100})
        groups={split:{r['requester_id'] for r in self.data['requests'] if splits[r['id']]==split}
                for split in ('DEV','TEST')}
        self.assertFalse(groups['DEV'] & groups['TEST'])
        profiles={p['id']:p for p in self.data['profiles']}
        requests={r['id']:r for r in self.data['requests']}
        for rid,uid,grade,reason in self.data['labels'][::100]:
            self.assertTrue(eligible_profile(requests[rid],profiles[uid]))
        self.assertTrue(all(not reviewed for *_,reviewed in self.data['cases']))

class MatchingMetricsTests(unittest.TestCase):
    def test_missed_positive_and_empty_negative_do_not_inflate_hit_rate(self):
        body=BackendMatchingRequest.model_validate({**BACKEND_MATCHING_EXAMPLE,
            'candidates':[dict(user_id=uid,nickname=str(uid),gender='FEMALE',
                theme=['NATURE','FOOD'],pace='BALANCED') for uid in range(2,8)]})
        case=dict(body=body,ranked=rank_candidates(body),
                  labels={2:0,3:0,4:0,5:0,6:0,7:2},expected_empty=False,
                  split='TEST',scenario='test',matching_request_id=1)
        score,_=metrics([case])
        self.assertEqual(score['hit_at_3'],0)
        negative={**case,'ranked':[],'labels':{},'expected_empty':True}
        combined,_=metrics([case,negative])
        self.assertEqual(combined['hit_at_3'],0)
        self.assertEqual((combined['negative_cases'],combined['negative_correct']),(1,1))
        self.assertEqual(combined['positive_cases'],1)

    def test_membership_counts_detect_extra_and_missing_candidates(self):
        body=BackendMatchingRequest.model_validate(BACKEND_MATCHING_EXAMPLE)
        case=dict(body=body,ranked=rank_candidates(body),labels={99:2},expected_empty=False,
                  split='TEST',scenario='test',matching_request_id=1)
        score,_=metrics([case])
        self.assertEqual(score['hard_condition_violations'],1)
        self.assertEqual(score['missing_eligible_candidates'],1)
