"""Real Redis and Celery worker integration; providers are mocked, E5 is local.

Run with REDIS_SERVER=/path/to/redis-server. No existing Redis or paid API is used.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
import redis

from ai_service.core.config import Settings
from ai_service.jobs.service import (
    JobService
)
from ai_service.jobs.store import RedisJobStore
from ai_service.jobs.worker import create_celery_app, publish, dispatch_pending
from ai_service.main import create_app
from ai_service.core.errors import ServiceUnavailable
from tests.travel.test_compact_routes import PlaceClient, Planner
from tests.travel.test_day_transport import request, http_request

REDIS_SERVER = os.environ.get('REDIS_SERVER') or shutil.which('redis-server')
HEADERS = {'Authorization': 'Bearer test-only'}


@unittest.skipUnless(REDIS_SERVER, 'Set REDIS_SERVER to test a real Redis/Celery deployment')
class QueueIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory(prefix='audigo-queue-')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.process = subprocess.Popen([
            REDIS_SERVER, '--bind', '127.0.0.1', '--port', str(cls.port),
            '--dir', cls.directory.name, '--appendonly', 'yes', '--appendfsync', 'always',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f'redis://127.0.0.1:{cls.port}'
        cls.settings = Settings(api_token='test-only', openai_api_key='test', kakao_rest_api_key='test',
                                jobs_redis_url=url + '/1', celery_broker_url=url + '/0', job_retry_delay_seconds=1)
        cls.store = RedisJobStore.from_settings(cls.settings)
        for _ in range(100):
            try:
                cls.store.client.ping()
                break
            except redis.exceptions.ConnectionError:
                time.sleep(0.05)
        else:
            cls.process.terminate()
            cls.process.wait()
            cls.directory.cleanup()
            raise RuntimeError('Test Redis failed to start')
        cls.celery = create_celery_app(cls.settings)
        cls.worker_context = start_worker(cls.celery, pool='solo', concurrency=1, perform_ping_check=False,
            queues=['audigo_control', 'audigo_itineraries'], shutdown_timeout=10)
        cls.worker_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.worker_context.__exit__(None, None, None)
        finally:
            cls.store.close()
            cls.process.terminate()
            cls.process.wait(timeout=10)
            cls.directory.cleanup()

    def app(self):
        app = create_app(settings=self.settings)
        return app

    def await_status(self, client, status_url):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = client.get(status_url, headers=HEADERS)
            self.assertEqual(response.status_code, 200, response.text)
            status = response.json()
            if status['status'] in {'COMPLETED', 'FAILED'}:
                return status
            time.sleep(0.1)
        self.fail('Queue worker did not finish in 30s')

    def test_itinerary_real_broker_worker_and_cross_api_result_retrieval(self):
        with patch('ai_service.jobs.worker.KakaoPlaces', return_value=PlaceClient()), \
             patch('ai_service.jobs.worker.OpenAIPlanner', return_value=Planner()):
            with TestClient(self.app()) as client:
                accepted = client.post('/api/ai/v1/itinerary-jobs', json=http_request(request(None, 'CAR')),
                                       headers={**HEADERS, 'Idempotency-Key': 'integration-trip'})
                self.assertEqual(accepted.status_code, 202, accepted.text)
                status = self.await_status(client, accepted.json()['status_url'])
                self.assertEqual(status['status'], 'COMPLETED', status)
                self.assertEqual(status['attempt'], 1)
                self.assertEqual(status['result']['music']['title'], '테스트 음악')
                duplicate = client.post('/api/ai/v1/itinerary-jobs', json=http_request(request(None, 'CAR')),
                                        headers={**HEADERS, 'Idempotency-Key': 'integration-trip'})
                self.assertEqual(duplicate.json()['job_id'], status['job_id'])
                publish(self.celery, self.store.get(status['job_id']))  # Duplicate broker delivery.
            # A different API process/client obtains the same result from shared Redis.
            with TestClient(self.app()) as other:
                again = other.get(status['status_url'], headers=HEADERS).json()
                self.assertEqual(again['result'], status['result'])
                self.assertEqual(again['attempt'], 1)

    def test_dispatcher_recovers_delivery_and_retry_without_losing_job_id(self):
        from ai_service.jobs import worker
        attempts = []
        async def flaky(doc, *args):
            attempts.append(doc['attempt'])
            if doc['attempt'] == 1:
                raise ServiceUnavailable(reason='provider_timeout')
            return {'days': []}
        service = JobService(self.store, lambda doc: None)  # Simulate no delivered Celery message.
        accepted = service.submit('ITINERARY_GENERATION', request(None, 'CAR').model_dump(mode='json'),
                                   {'generation_job_id': 1301}, 'req-retry', 'integration-retry')
        # Restore an undelivered outbox record to exercise the dispatcher.
        self.store.client.zadd(self.store.outbox, {accepted.job_id: 0})
        with patch('ai_service.jobs.worker.run_pipeline', flaky):
            dispatch_pending(self.celery, self.store)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                doc = self.store.get(accepted.job_id)
                if doc['status'] == 'COMPLETED':
                    break
                dispatch_pending(self.celery, self.store)
                time.sleep(0.1)
            else:
                self.fail('Retry did not complete')
        self.assertEqual(doc['status'], 'COMPLETED', doc)
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(doc['attempt'], 2)
        self.assertEqual(self.store.client.zscore(self.store.outbox, accepted.job_id), None)
