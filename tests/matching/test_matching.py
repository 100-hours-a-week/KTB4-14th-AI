"""매칭 계산식 점수·필터·일관된 정렬 순서를 검증한다."""
from copy import deepcopy
import unittest

from ai_service.api.examples.matching import BACKEND_MATCHING_EXAMPLE
from ai_service.schemas.matching import BackendMatchingRequest
from ai_service.services.matching import match_rate
from ai_service.services.matching import recommend


class MatchingScoringTests(unittest.TestCase):
    def test_exact_scores_and_half_up_rounding(self):
        cases = [
            (['NATURE','FOOD'],['NATURE','FOOD'],'BALANCED','BALANCED',100),
            (['NATURE','FOOD'],['NATURE'],'BALANCED','BALANCED',60),
            (['NATURE','FOOD'],['NATURE','FOOD'],'BALANCED','RELAXED',90),
            (['NATURE','FOOD'],['NATURE'],'BALANCED','RELAXED',50),
            (['NATURE','FOOD'],['CULTURE'],'BALANCED','BALANCED',20),
            (['NATURE','FOOD'],['CULTURE'],'BALANCED','PACKED',10),
            (['NATURE','FOOD','REST'],['NATURE'],'BALANCED','BALANCED',47),
            (['NATURE','FOOD','REST'],['NATURE','REST'],'BALANCED','BALANCED',73),
            (['NATURE'],['NATURE','FOOD','REST','SNS'],'RELAXED','RELAXED',100),
            (['NATURE'],['NATURE'],'RELAXED','PACKED',None),
            (['NATURE'],['NATURE'],'PACKED','RELAXED',None),
        ]
        for themes,offered,pace,other,expected in cases:
            with self.subTest(themes=themes,offered=offered,pace=pace,other=other):
                self.assertEqual(match_rate(themes,offered,pace,other),expected)

    def test_rank_order_uses_scores_then_nickname_then_id_and_excludes_self_gender_pace(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        body['request']['pace']='RELAXED'
        body['candidates']=[dict(user_id=uid,nickname=name,gender=gender,theme=themes,pace=pace)
            for uid,name,gender,themes,pace in [
                (1,'본인','FEMALE',['NATURE','FOOD'],'RELAXED'),
                (2,'가','MALE',['NATURE','FOOD'],'RELAXED'),
                (3,'가','FEMALE',['NATURE','FOOD'],'PACKED'),
                (4,'가','FEMALE',['NATURE'],'RELAXED'),
                (5,'나','FEMALE',['NATURE','FOOD'],'BALANCED'),
                (7,'가','FEMALE',['NATURE','FOOD'],'BALANCED'),
                (6,'가','FEMALE',['NATURE','FOOD'],'BALANCED')]]
        result=recommend(BackendMatchingRequest.model_validate(body))
        self.assertEqual(result.model_dump(),{'candidates':[
            {'user_id':6,'match_rate':90},{'user_id':7,'match_rate':90},{'user_id':5,'match_rate':90}], 'count':3})

    def test_missing_candidate_budgets_keep_existing_scores(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        for candidate in body['candidates']:
            candidate.pop('budget_min',None)
            candidate.pop('budget_max',None)
        original=recommend(BackendMatchingRequest.model_validate(body))
        for budgets in ((None,None),(0,3000000),(2000000,3000000)):
            body['request']['budget_min'],body['request']['budget_max']=budgets
            self.assertEqual(recommend(BackendMatchingRequest.model_validate(body)),original)
