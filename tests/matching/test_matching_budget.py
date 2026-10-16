"""예산 범위·최대 5점 감점·기존 요청 호환성·후보 예산 검증을 확인한다."""
from copy import deepcopy
import unittest

from fastapi.testclient import TestClient

from ai_service.api.examples.matching import BUDGET_MATCHING_EXAMPLE
from ai_service.core.config import Settings
from ai_service.main import create_app
from ai_service.schemas.matching import BackendMatchingRequest
from ai_service.services.matching import match_rate, recommend


class MatchingBudgetTests(unittest.TestCase):
    def score(self, requested, offered, themes=('NATURE', 'FOOD'), other=('NATURE', 'FOOD')):
        return match_rate(themes, other, 'BALANCED', 'BALANCED',
                          request_budget_min=requested[0], request_budget_max=requested[1],
                          candidate_budget_min=offered[0], candidate_budget_max=offered[1])

    def test_overlap_partial_disjoint_and_touching_ranges(self):
        for offered, expected in [((100000,800000),100), ((200000,300000),100),
                                  ((0,1000000),100), ((450000,1150000),98),
                                  ((900000,1600000),95), ((800000,1000000),95)]:
            with self.subTest(offered=offered):
                self.assertEqual(self.score((100000,800000),offered),expected)

    def test_fixed_amounts_and_zero_budget(self):
        for requested, offered, expected in [((0,0),(0,0),100), ((0,0),(1,1),95),
                ((0,0),(0,100000),100), ((100000,800000),(300000,300000),100),
                ((100000,800000),(900000,900000),95), ((300000,300000),(100000,800000),100)]:
            with self.subTest(requested=requested,offered=offered):
                self.assertEqual(self.score(requested,offered),expected)

    def test_missing_request_or_candidate_budgets_do_not_change_scores(self):
        for requested,offered in [((None,None),(100000,800000)),
                ((100000,800000),(None,None)),((None,None),(None,None))]:
            self.assertEqual(self.score(requested,offered),100)

    def test_budget_never_changes_score_by_more_than_five_points(self):
        for themes in [('NATURE',),('NATURE','FOOD'),('NATURE','FOOD','REST')]:
            for offered_themes in [('NATURE',),('FOOD','REST'),('CULTURE',)]:
                original = match_rate(themes,offered_themes,'BALANCED','BALANCED')
                for offered in [(100000,800000),(450000,1150000),(900000,1600000)]:
                    adjusted=self.score((100000,800000),offered,themes,offered_themes)
                    self.assertTrue(0 <= original-adjusted <= 5)
                    self.assertTrue(0 <= adjusted <= 100)

    def test_budget_ranks_equal_profiles_but_does_not_exclude_disjoint_candidate(self):
        result=recommend(BackendMatchingRequest.model_validate(BUDGET_MATCHING_EXAMPLE))
        self.assertEqual(result.model_dump(),{'candidates':[
            {'user_id':23,'match_rate':100},{'user_id':24,'match_rate':98},
            {'user_id':25,'match_rate':95}], 'count':3})

    def test_budget_cannot_bypass_gender_self_or_opposite_pace_filters(self):
        body=deepcopy(BUDGET_MATCHING_EXAMPLE)
        body['request']['pace']='RELAXED'
        body['candidates']=[dict(body['candidates'][0],user_id=uid,gender=gender,pace=pace)
            for uid,gender,pace in [(1,'FEMALE','RELAXED'),(22,'MALE','RELAXED'),(23,'FEMALE','PACKED')]]
        self.assertEqual(recommend(BackendMatchingRequest.model_validate(body)).count,0)

    def test_candidate_budget_errors_are_http_400_and_legacy_candidates_are_accepted(self):
        path='/api/ai/v1/matches/candidates'
        headers={'Authorization':'Bearer budget-test-only'}
        with TestClient(create_app(settings=Settings(api_token='budget-test-only'))) as client:
            for low,high in [(100000,None),(None,800000),(-1,800000),(0,3000001),
                             (900000,800000),('100000',800000),(True,800000),(1.5,800000)]:
                body=deepcopy(BUDGET_MATCHING_EXAMPLE)
                body['candidates'][0].update(budget_min=low,budget_max=high)
                with self.subTest(low=low,high=high):
                    response=client.post(path,json=body,headers=headers)
                    self.assertEqual(response.status_code,400,response.text)
                    self.assertEqual(response.json()['error']['code'],'INVALID_REQUEST')
            for explicit_null in (False,True):
                body=deepcopy(BUDGET_MATCHING_EXAMPLE)
                for candidate in body['candidates']:
                    if explicit_null:
                        candidate.update(budget_min=None,budget_max=None)
                    else:
                        candidate.pop('budget_min')
                        candidate.pop('budget_max')
                response=client.post(path,json=body,headers=headers)
                self.assertEqual(response.status_code,200,response.text)
                self.assertTrue(all(c['match_rate']==100 for c in response.json()['candidates']))
