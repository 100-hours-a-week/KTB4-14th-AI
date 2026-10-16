"""매칭 API의 경로·Swagger 스키마·요청과 오류 응답 예시를 검증한다."""
import unittest
from copy import deepcopy
from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_service.core.config import Settings
from ai_service.main import create_app
from ai_service.api.examples.matching import BACKEND_MATCHING_EXAMPLE, BUDGET_MATCHING_EXAMPLE


class MatchingOpenAPITests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings=Settings())
        with TestClient(self.app) as client:
            self.spec = client.get("/openapi.json").json()
        self.operation = self.spec["paths"]["/api/ai/v1/matches/candidates"]["post"]

    def test_backend_matching_uses_new_path_and_old_paths_are_removed(self):
        self.assertFalse(any(route.path == "/matching-requests" for route in self.app.routes))
        self.assertNotIn("/internal/ai/matching/recommend", self.spec["paths"])
        self.assertNotIn("/matching-requests", self.spec["paths"])
        self.assertNotIn("/api/ai/v1/matching-requests", self.spec["paths"])
        self.assertEqual(self.operation["operationId"], "matching_candidates_api_ai_v1_matches_candidates_post")
        self.assertEqual(self.operation["tags"], ["V2"])

    def test_single_bearer_scheme_is_used(self):
        self.assertEqual(self.operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(set(self.spec["components"]["securitySchemes"]), {"HTTPBearer"})

    def test_previous_paths_return_404_and_have_no_runtime_services(self):
        with TestClient(self.app) as client:
            for path in ("/matching-requests", "/api/ai/v1/matching-requests"):
                for headers in ({}, {"Authorization": "Bearer test-only"}):
                    response = client.post(path, json={}, headers=headers)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json(), {"message": "not_found", "data": None})
            self.assertFalse(hasattr(self.app.state, "matching_recommender"))
            self.assertFalse(hasattr(self.app.state, "matching_candidate_source"))

    def test_matching_queue_route_is_removed_from_runtime_and_swagger(self):
        path = "/api/ai/v1/traveler-match-jobs"
        self.assertNotIn(path, self.spec["paths"])
        self.assertFalse(any(route.path == path for route in self.app.routes))
        with TestClient(self.app) as client:
            for headers in ({}, {"Authorization": "Bearer test-only"}):
                response = client.post(path, json={}, headers=headers)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"message": "not_found", "data": None})

    def test_response_examples_match_actual_api_responses(self):
        path = '/api/ai/v1/matches/candidates'
        headers = {'Authorization': 'Bearer test-only'}
        app = create_app(settings=Settings(api_token='test-only'))
        with TestClient(app, raise_server_exceptions=False) as client:
            documented = client.get('/openapi.json').json()['paths'][path]['post']['responses']

            def assert_example(response, code, name):
                self.assertEqual(response.status_code, code, response.text)
                actual = response.json()
                if 'request_id' in actual:
                    self.assertTrue(actual['request_id'].startswith('req_'))
                    actual['request_id'] = 'req_matching_example'
                expected = documented[str(code)]['content']['application/json']['examples'][name]['value']
                self.assertEqual(actual, expected)

            assert_example(client.post(path,json=BACKEND_MATCHING_EXAMPLE,headers=headers),200,'recommended')
            assert_example(client.post(path,json=BUDGET_MATCHING_EXAMPLE,headers=headers),200,'budget_adjusted')
            body = deepcopy(BACKEND_MATCHING_EXAMPLE)
            body['candidates'] = [dict(user_id=uid,nickname=str(uid),gender='FEMALE',theme=themes,pace=pace)
                for uid,themes,pace in [(23,['NATURE','FOOD'],'BALANCED'),
                    (24,['NATURE','FOOD'],'RELAXED'),(25,['NATURE'],'BALANCED')]]
            assert_example(client.post(path,json=body,headers=headers),200,'top_three')
            assert_example(client.post(path,json={**body,'candidates':[]},headers=headers),200,'empty')

            mutations = {
                'gender_null': lambda b: b['request'].update(preferred_companion_gender=None),
                'theme_null': lambda b: b['request'].update(theme=None),
                'pace_null': lambda b: b['request'].update(pace=None),
                'candidates_null': lambda b: b.update(candidates=None),
                'nickname_missing': lambda b: b['candidates'][0].pop('nickname'),
                'limit_exceeded': lambda b: b.update(limit=4),
            }
            for name, mutate in mutations.items():
                body = deepcopy(BACKEND_MATCHING_EXAMPLE)
                mutate(body)
                with self.subTest(example=name):
                    assert_example(client.post(path,json=body,headers=headers),400,name)
            assert_example(client.post(path,json=BACKEND_MATCHING_EXAMPLE),401,'unauthorized')
            with patch('ai_service.api.routes.matching.recommend',side_effect=RuntimeError('test failure')):
                assert_example(client.post(path,json=BACKEND_MATCHING_EXAMPLE,headers=headers),500,'internal_error')
        with TestClient(create_app(settings=Settings())) as client:
            assert_example(client.post(path,json=BACKEND_MATCHING_EXAMPLE,headers=headers),503,'service_unavailable')


if __name__ == "__main__":
    unittest.main()
