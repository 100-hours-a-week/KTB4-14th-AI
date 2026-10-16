"""운영 로그의 한국어 원인·조치, 요청 추적과 비밀값 제외를 확인한다."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_service.core.config import Settings
from ai_service.core.diagnostics import failure
from ai_service.core.errors import ServiceUnavailable
from ai_service.main import create_app
from ai_service.api.examples.matching import BACKEND_MATCHING_EXAMPLE

PATH = '/api/ai/v1/matches/candidates'
HEADERS = {'Authorization': 'Bearer test-only'}


class KoreanErrorLogTests(unittest.TestCase):
    def entries(self, logs):
        return [json.loads(r.getMessage()) for r in logs.records]

    def test_matching_validation_logs_field_reason_and_action_without_input_values(self):
        body = deepcopy(BACKEND_MATCHING_EXAMPLE)
        body['request']['theme'] = None
        body['limit'] = 4
        body['candidates'][0]['nickname'] = 'private_input_name'
        with TestClient(create_app(settings=Settings(api_token='test-only'))) as client, \
             self.assertLogs('uvicorn.error.audigo',level='WARNING') as logs:
            response = client.post(PATH+'?token=private_query_token',json=body,headers=HEADERS)
        self.assertEqual(response.status_code,400)
        entry = self.entries(logs)[0]
        self.assertEqual(entry['event'],'request_invalid')
        self.assertEqual(entry['request_id'],response.json()['request_id'])
        self.assertEqual((entry['method'],entry['path']),('POST',PATH))
        errors = {error['field']:error for error in entry['errors']}
        self.assertIn('null',errors['body.request.theme']['message_ko'])
        self.assertIn('배열',errors['body.request.theme']['action_ko'])
        self.assertIn('1~3',errors['body.limit']['action_ko'])
        self.assertEqual(errors['body.limit']['message_ko'],'추천 수: 3 이하여야 합니다.')
        self.assertEqual(errors['body.limit']['limits'],{'le':3})
        text = json.dumps(entry,ensure_ascii=False)
        self.assertNotIn('private_input_name',text)
        self.assertNotIn('private_query_token',text)

    def test_authentication_logs_service_token_cause_without_token_value(self):
        with TestClient(create_app(settings=Settings(api_token='test-only'))) as client, \
             self.assertLogs('uvicorn.error.audigo',level='WARNING') as logs:
            response = client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,
                                   headers={'Authorization':'Bearer private_header_token'})
        entry = self.entries(logs)[0]
        self.assertEqual(response.status_code,401)
        self.assertEqual(entry['reason'],'service_token_invalid')
        self.assertEqual(entry['request_id'],response.json()['request_id'])
        self.assertIn('Authorization',entry['cause_ko'])
        self.assertNotIn('private_header_token',json.dumps(entry))

    def test_missing_server_token_log_distinguishes_configuration_from_queue_outage(self):
        with TestClient(create_app(settings=Settings())) as client, \
             self.assertLogs('uvicorn.error.audigo',level='ERROR') as logs:
            response = client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS)
        entry = self.entries(logs)[0]
        self.assertEqual(response.status_code,503)
        self.assertEqual(entry['code'],response.json()['error']['code'])
        self.assertEqual(entry['reason'],'service_token_missing')
        self.assertIn('AUDIGO_API_TOKEN',entry['cause_ko'])
        self.assertIn('서비스 토큰',entry['action_ko'])
        self.assertNotIn('Redis',entry['cause_ko'])

    def test_internal_error_preserves_request_id_and_excludes_exception_text(self):
        with TestClient(create_app(settings=Settings(api_token='test-only')),raise_server_exceptions=False) as client, \
             patch('ai_service.api.routes.matching.recommend',side_effect=RuntimeError('private_exception_token')), \
             self.assertLogs('uvicorn.error.audigo',level='ERROR') as logs:
            response = client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS)
        entry = self.entries(logs)[0]
        self.assertEqual(response.status_code,500)
        self.assertEqual(entry['request_id'],response.json()['request_id'])
        self.assertEqual(entry['http_status'],500)
        self.assertEqual(entry['error_type'],'RuntimeError')
        self.assertTrue(entry['frames'])
        self.assertIn('파일·줄 번호',entry['action_ko'])
        self.assertNotIn('private_exception_token',json.dumps(entry))

    def test_unregistered_path_and_wrong_method_have_korean_explanations(self):
        with TestClient(create_app(settings=Settings(api_token='test-only'))) as client:
            with self.assertLogs('uvicorn.error.audigo',level='WARNING') as logs:
                response = client.get('/private_path_token?secret=private_query_token')
            entry = self.entries(logs)[0]
            self.assertEqual(response.status_code,404)
            self.assertEqual(entry['code'],'NOT_FOUND')
            self.assertIn('API 경로',entry['message_ko'])
            self.assertNotIn('private_path_token',json.dumps(entry))
            self.assertNotIn('private_query_token',json.dumps(entry))
            with self.assertLogs('uvicorn.error.audigo',level='WARNING') as logs:
                response = client.get(PATH,headers=HEADERS)
            entry = self.entries(logs)[0]
            self.assertEqual(response.status_code,405)
            self.assertEqual(entry['reason'],'method_not_allowed')
            self.assertIn('POST',entry['action_ko'])

    def test_provider_and_music_errors_have_specific_actions(self):
        for reason,expected in [('music_model_load_failed','E5_MODEL_DIR'),
                                ('openai_request_failed','호출 한도'),
                                ('job_storage_unavailable','JOBS_REDIS_URL')]:
            with self.subTest(reason=reason), self.assertLogs('uvicorn.error.audigo',level='ERROR') as logs:
                failure('pipeline_failed',ServiceUnavailable(reason=reason))
            self.assertIn(expected,self.entries(logs)[0]['action_ko'])

    def test_duplicate_candidates_have_specific_korean_cause(self):
        body = deepcopy(BACKEND_MATCHING_EXAMPLE)
        body['candidates'].append(deepcopy(body['candidates'][0]))
        with TestClient(create_app(settings=Settings(api_token='test-only'))) as client, \
             self.assertLogs('uvicorn.error.audigo',level='WARNING') as logs:
            response = client.post(PATH,json=body,headers=HEADERS)
        self.assertEqual(response.status_code,400)
        self.assertIn('후보 사용자 ID가 중복',self.entries(logs)[0]['errors'][0]['message_ko'])
