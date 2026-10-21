"""기존 큐 HTTP API 제거와 SQS·Redis 설정에 독립적인 HTTP 앱 시작을 검증한다."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_service.core.config import Settings
from ai_service.main import create_app


class HttpBoundaryTests(unittest.TestCase):
    def test_removed_queue_routes_return_404_and_are_absent_from_openapi(self):
        with TestClient(create_app(settings=Settings(api_token='test-only'))) as client:
            paths=client.get('/openapi.json').json()['paths']
            self.assertNotIn('/api/ai/v1/itinerary-jobs',paths)
            self.assertNotIn('/api/ai/v1/jobs/{job_id}',paths)
            self.assertIn('/api/ai/v1/itinerary-jobs/stream',paths)
            for method,path in [('POST','/api/ai/v1/itinerary-jobs'),
                                ('GET','/api/ai/v1/jobs/old-job')]:
                for headers in ({},{'Authorization':'Bearer test-only'}):
                    response=client.request(method,path,headers=headers)
                    self.assertEqual(response.status_code,404)
                    self.assertEqual(response.json(),{'message':'not_found','data':None})
            self.assertFalse(hasattr(client.app.state,'jobs'))

    def test_http_startup_does_not_connect_to_sqs_or_optional_redis(self):
        with patch('boto3.client',side_effect=AssertionError('Unexpected AWS connection')), \
             patch('redis.Redis.from_url',side_effect=AssertionError('Unexpected Redis connection')):
            settings=Settings(api_token='test-only',jobs_redis_url='redis://unused.test/1')
            with TestClient(create_app(settings=settings)) as client:
                self.assertEqual(client.get('/health').status_code,200)
                self.assertEqual(client.get('/docs').status_code,200)
