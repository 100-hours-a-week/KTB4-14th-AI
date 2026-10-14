"""Synthetic data reproducibility and the actual ranking policy's boundaries."""
import asyncio
from collections import Counter
import json
from pathlib import Path
import unittest

import numpy as np
from pydantic import ValidationError

from ai_service.core.config import Settings
from ai_service.jobs.schemas import DatabaseMatchingRecommendationRequest, MatchingJobRequest
from ai_service.matching.service import E5MatchingRecommender
from ai_service.matching.dev import DATA_DIR, eligible_profile, make_dataset
from scripts.matching.evaluate_matching_dev import metrics, select_weights


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

class MatchingRankingTests(unittest.TestCase):
    def body(self, count=2):
        return DatabaseMatchingRecommendationRequest(matching_request_id=1,travel_plan_id=1,
            requester_id=1,preferred_companion_gender='ANY',pace='BALANCED',themes=['nature'],limit=5,
            candidates=[dict(user_id=uid+2,pace='BALANCED',themes=['nature'],introduction=f'candidate {uid+2}')
                        for uid in range(count)])

    def test_global_top_five_includes_best_candidate_after_first_embedding_batch(self):
        class Encoder:
            def __init__(self): self.calls=[]
            def encode(self,texts):
                self.calls.append(texts)
                return np.array([[1,0]]+[[1,0] if text.endswith('candidate 406') else [0,1]
                                        for text in texts[1:]],dtype=np.float32)
        encoder=Encoder()
        body=self.body(405)
        result=asyncio.run(E5MatchingRecommender(Path('/unused'),encoder=encoder).recommend(body))
        self.assertEqual([r.user_id for r in result.data.recommendations],[406,2,3,4,5])
        self.assertEqual([len(batch) for batch in encoder.calls],[201,201,6])
        self.assertTrue(all(batch[0].startswith('query: ') for batch in encoder.calls))
        self.assertTrue(all(text.startswith('passage: ') for batch in encoder.calls for text in batch[1:]))
        with self.assertRaises(ValidationError):
            MatchingJobRequest.model_validate(body.model_dump())

    def test_hybrid_can_correct_semantic_ranking_and_default_retains_cosine(self):
        body=self.body()
        body.candidates[0].theme=['city']
        body.candidates[0].pace='Packed'
        cosine=np.array([0.9,0.8])
        np.testing.assert_array_equal(E5MatchingRecommender.combine_scores(body,body.candidates,cosine,(1.,0.,0.)),cosine)
        hybrid=E5MatchingRecommender.combine_scores(body,body.candidates,cosine,(.85,.1,.05))
        self.assertGreater(hybrid[1],hybrid[0])
        self.assertTrue(np.all((0<=hybrid)&(hybrid<=1)))

    def test_weight_validation_prevents_invalid_ranking_configuration(self):
        for weights in ((.8,.5,0),(-.1,.6,.5),(float('nan'),0,1)):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                Settings(matching_e5_weight=weights[0],matching_theme_weight=weights[1],matching_pace_weight=weights[2])

    def test_metrics_count_missed_positive_and_empty_negative_without_inflating_hit_rate(self):
        body=self.body(6)
        case=dict(body=body,candidates=body.candidates,cosine=np.arange(6)[::-1]/6,
                  labels={2:0,3:0,4:0,5:0,6:0,7:2},expected_empty=False,
                  split='DEV',scenario='test',region_id=1)
        baseline,_=metrics([case],(1.,0.,0.))
        self.assertEqual(baseline['hit_at_3'],0)
        self.assertEqual(baseline['positive_cases'],1)
        negative={**case,'candidates':[],'cosine':np.asarray([]),'labels':{},'expected_empty':True}
        combined,_=metrics([case,negative],(1.,0.,0.))
        self.assertEqual(combined['hit_at_3'],0)
        self.assertEqual((combined['negative_cases'],combined['negative_correct']),(1,1))
        self.assertEqual(combined['positive_cases'],1)
        weights,comparisons=select_weights([case])
        self.assertEqual(len(comparisons),5)
        self.assertIn(list(weights),[list(row['weights']) for row in comparisons])
