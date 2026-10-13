"""HTTP contracts, Redis atomicity, retries, recovery and existing pipeline reuse."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import fakeredis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from ai_service.config import Settings
from ai_service.errors import GenerationFailed, ServiceUnavailable
from ai_service.jobs.api import JobService, MATCHING_EXAMPLE
from ai_service.jobs.store import RedisJobStore
from ai_service.jobs.worker import dispatch_pending, execute_job, run_pipeline
from ai_service.main import create_app
from test_day_transport import http_request, request
from test_compact_routes import PlaceClient, Planner

HEADERS = {'Authorization': 'Bearer test-only'}
MATCH_PATH = '/api/ai/v1/matches/candidates'
TRIP_PATH = '/api/ai/v1/itinerary-jobs'


def new_store(**kwargs):
    return RedisJobStore(fakeredis.FakeRedis(decode_responses=True), **kwargs)


def reserve(store, key='one', payload=None):
    return store.reserve('TRAVELER_MATCHING', payload or MATCHING_EXAMPLE,
                         {'matching_request_id': 201, 'travel_plan_id': 101}, 'req-job', key)[0]


class JobStoreTests(unittest.TestCase):
    def test_same_key_same_payload_is_one_job_and_changed_payload_conflicts(self):
        store = new_store()
        docs = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            docs = list(pool.map(lambda _: reserve(store), range(20)))
        self.assertEqual(len({d['job_id'] for d in docs}), 1)
        self.assertEqual(store.client.zcard(store.outbox), 1)
        from ai_service.jobs.errors import JobApiError
        with self.assertRaises(JobApiError) as error:
            reserve(store, payload={'changed': True})
        self.assertEqual(error.exception.status_code, 409)

    def test_lease_allows_one_execution_and_terminal_result_cannot_be_overwritten(self):
        store = new_store()
        doc = reserve(store)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda n: store.claim(doc['job_id'], f'owner-{n}'), range(20)))
        winner = next(c for c in claims if c)
        self.assertEqual(sum(c is not None for c in claims), 1)
        self.assertIsNone(store.finish(doc['job_id'], 'wrong', 'COMPLETED', result={'bad': True}))
        store.finish(doc['job_id'], winner['lease_token'], 'COMPLETED', result={'ok': True})
        self.assertIsNone(store.claim(doc['job_id'], 'redelivery'))
        self.assertIsNone(store.finish(doc['job_id'], winner['lease_token'], 'FAILED'))
        self.assertEqual(store.get(doc['job_id'])['result'], {'ok': True})
        self.assertEqual(store.client.zcard(store.outbox), 0)

    def test_expired_worker_lease_is_recovered_and_old_worker_is_fenced(self):
        store = new_store(lease_seconds=1, max_attempts=2)
        with patch('ai_service.jobs.store.time.time', return_value=1000):
            job_id = reserve(store)['job_id']
            store.claim(job_id, 'dead')
        with patch('ai_service.jobs.store.time.time', return_value=1002):
            self.assertEqual(store.due()[0][0], job_id)
            recovered = store.claim(job_id, 'new')
            self.assertEqual(recovered['attempt'], 2)
            self.assertIsNone(store.finish(job_id, 'dead', 'COMPLETED', result={'stale': True}))
        with patch('ai_service.jobs.store.time.time', return_value=1004):
            self.assertIsNone(store.claim(job_id, 'another-crash'))
            status = store.public(store.get(job_id))
            self.assertEqual(status.status, 'FAILED')
            self.assertEqual(status.error.code, 'WORKER_ATTEMPTS_EXHAUSTED')

    def test_outbox_survives_publication_failure_and_dispatches_again(self):
        store = new_store()
        publisher = Mock(side_effect=RuntimeError('broker outage'))
        with self.assertLogs('ai_service.jobs.api', level='WARNING'):
            accepted = JobService(store, publisher).submit('TRAVELER_MATCHING', MATCHING_EXAMPLE,
                {'travel_plan_id': 101}, 'req', 'key')
        self.assertEqual(accepted.status, 'QUEUED')
        self.assertEqual(len(store.due()), 1)
        app = Mock()
        self.assertEqual(dispatch_pending(app, store), 1)
        self.assertEqual(app.send_task.call_args.kwargs['args'], [accepted.job_id])
        self.assertEqual(store.client.zcard(store.outbox), 0)  # Confirmed jobs need no repeated publication.

    def test_publication_confirmation_cannot_remove_a_worker_recovery_lease(self):
        store = new_store()
        doc = reserve(store, payload={'required_places': [], 'large_id': 9223372036854775807})
        score = store.client.zscore(store.outbox, doc['job_id'])
        store.claim(doc['job_id'], 'owner')
        store.dispatched(doc['job_id'], score)
        self.assertEqual(store.client.zcard(store.outbox), 1)
        self.assertEqual(store.get(doc['job_id'])['payload']['required_places'], [])
        store.finish(doc['job_id'], 'owner', 'COMPLETED', result={'recommendations': []})
        self.assertEqual(store.get(doc['job_id'])['result']['recommendations'], [])

    def test_business_ids_preserve_java_long_precision_after_updates(self):
        store = new_store()
        doc, _ = store.reserve('TRAVELER_MATCHING', MATCHING_EXAMPLE,
                              {'travel_plan_id': 9223372036854775807}, 'req-long', 'long-id')
        store.claim(doc['job_id'], 'owner')
        store.finish(doc['job_id'], 'owner', 'COMPLETED', result={'recommendations': []})
        self.assertEqual(store.public(store.get(doc['job_id'])).travel_plan_id, 9223372036854775807)

    def test_record_ttl_and_reconnecting_status_store(self):
        server = fakeredis.FakeServer()
        store = RedisJobStore(fakeredis.FakeRedis(server=server, decode_responses=True))
        job_id = reserve(store)['job_id']
        self.assertGreater(store.client.ttl(store.key(job_id)), 86000)
        other = RedisJobStore(fakeredis.FakeRedis(server=server, decode_responses=True))
        self.assertEqual(other.get(job_id)['status'], 'QUEUED')
        store.client.delete(store.key(job_id))
        self.assertIsNone(other.get(job_id))


class JobApiTests(unittest.TestCase):
    def setUp(self):
        self.store, self.publisher = new_store(), Mock()
        self.app = create_app(settings=Settings(api_token='test-only', openai_api_key='test', kakao_rest_api_key='test'))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.app.state.jobs = JobService(self.store, self.publisher)

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_accept_matching_poll_and_hide_input_internal_tokens(self):
        response = self.client.post(MATCH_PATH, json=MATCHING_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 202, response.text)
        accepted = response.json()
        self.assertEqual(set(accepted), {'job_id', 'job_type', 'status', 'status_url'})
        self.assertEqual(accepted['job_type'], 'TRAVELER_MATCHING')
        status = self.client.get(accepted['status_url'], headers=HEADERS).json()
        self.assertEqual(status['matching_request_id'], 201)
        self.assertEqual(status['status'], 'QUEUED')
        self.assertIsNone(status['result'])
        self.assertFalse({'payload', 'fingerprint', 'lease_token', 'requester_id'} & set(status))
        self.publisher.assert_called_once()

    def test_both_itinerary_dtos_are_accepted_without_inference_in_http(self):
        legacy = request(None, 'CAR').model_dump(mode='json')
        legacy.pop('travel_plan_id')
        for body in (legacy, http_request(request(None, 'CAR'))):
            response = self.client.post(TRIP_PATH, json=body, headers=HEADERS)
            self.assertEqual(response.status_code, 202, response.text)
            doc = self.store.get(response.json()['job_id'])
            self.assertEqual(doc['job_type'], 'ITINERARY_GENERATION')
            self.assertEqual(doc['attempt'], 0)

    def test_duplicate_request_and_conflict_and_explicit_new_key(self):
        first = self.client.post(MATCH_PATH, json=MATCHING_EXAMPLE, headers=HEADERS).json()
        second = self.client.post(MATCH_PATH, json=MATCHING_EXAMPLE, headers=HEADERS).json()
        self.assertEqual(first['job_id'], second['job_id'])
        self.publisher.assert_called_once()
        changed = {**MATCHING_EXAMPLE, 'limit': 1}
        conflict = self.client.post(MATCH_PATH, json=changed, headers=HEADERS)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['error']['code'], 'IDEMPOTENCY_CONFLICT')
        fresh = self.client.post(MATCH_PATH, json=changed, headers={**HEADERS, 'Idempotency-Key': 'new-generation'})
        self.assertEqual(fresh.status_code, 202)
        self.assertNotEqual(fresh.json()['job_id'], first['job_id'])

    def test_400_401_404_422_503_have_job_contract_and_request_id(self):
        cases = [
            self.client.post(MATCH_PATH, json={}, headers=HEADERS),
            self.client.post(MATCH_PATH, json=MATCHING_EXAMPLE),
            self.client.get('/api/ai/v1/jobs/unknown', headers=HEADERS),
            self.client.post(MATCH_PATH, json={**MATCHING_EXAMPLE, 'candidates': []}, headers=HEADERS),
        ]
        self.app.state.jobs = None
        cases.append(self.client.post(MATCH_PATH, json=MATCHING_EXAMPLE, headers=HEADERS))
        self.assertEqual([r.status_code for r in cases], [400, 401, 404, 422, 503])
        for response in cases:
            self.assertEqual(set(response.json()), {'error', 'request_id'})
            self.assertEqual(response.json()['request_id'], response.headers['x-request-id'])
            self.assertIsInstance(response.json()['error']['details'], list)

    def test_redis_outage_returns_503_without_accepted_job(self):
        with patch.object(self.store, 'reserve', side_effect=RedisConnectionError('secret redis URI')):
            response = self.client.post(MATCH_PATH, json=MATCHING_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('secret', response.text)
        self.publisher.assert_not_called()

    def test_budget_gender_self_and_duplicate_filters(self):
        for candidates in ([], [{**MATCHING_EXAMPLE['candidates'][0], 'user_id': 12}],
                           [{**MATCHING_EXAMPLE['candidates'][0], 'budget_min': None, 'budget_max': None}]):
            response = self.client.post(MATCH_PATH, json={**MATCHING_EXAMPLE, 'candidates': candidates}, headers=HEADERS)
            self.assertEqual(response.status_code, 422)
        response = self.client.post(MATCH_PATH, json={**MATCHING_EXAMPLE, 'preferred_companion_gender': 'Female'}, headers=HEADERS)
        self.assertEqual(response.status_code, 422)  # Missing gender is not guessed.
        duplicate = {**MATCHING_EXAMPLE, 'candidates': MATCHING_EXAMPLE['candidates'] * 2}
        self.assertEqual(self.client.post(MATCH_PATH, json=duplicate, headers=HEADERS).status_code, 400)


class JobExecutionTests(unittest.TestCase):
    def test_retry_backoff_then_success_and_duplicate_delivery_do_not_rerun(self):
        store, settings = new_store(), Settings(job_retry_delay_seconds=1)
        job_id = reserve(store)['job_id']
        runner = AsyncMock(side_effect=[ServiceUnavailable(reason='provider_timeout'), {'recommendations': []}])
        with patch('ai_service.jobs.worker.run_pipeline', runner):
            execute_job(job_id, store, settings)
            doc = store.get(job_id)
            self.assertEqual(doc['status'], 'RETRYING')
            self.assertEqual(doc['attempt'], 1)
            execute_job(job_id, store, settings)  # Delivery before next_attempt_at cannot execute.
            self.assertEqual(runner.await_count, 1)
            with patch('ai_service.jobs.store.time.time', return_value=doc['next_attempt_at'] + 0.1):
                execute_job(job_id, store, settings)
            execute_job(job_id, store, settings)
        self.assertEqual(runner.await_count, 2)
        self.assertEqual(store.get(job_id)['status'], 'COMPLETED')
        self.assertEqual(store.get(job_id)['attempt'], 2)

    def test_retry_exhaustion_and_permanent_error(self):
        for error, expected_attempts in ((GenerationFailed(), 1),
                                         (ServiceUnavailable(reason='matching_model_load_failed'), 1),
                                         (ServiceUnavailable(reason='provider_timeout'), 2)):
            store = new_store(max_attempts=2)
            job_id = reserve(store)['job_id']
            with patch('ai_service.jobs.worker.run_pipeline', AsyncMock(side_effect=error)) as runner:
                execute_job(job_id, store, Settings())
                doc = store.get(job_id)
                if doc['status'] == 'RETRYING':
                    with patch('ai_service.jobs.store.time.time', return_value=doc['next_attempt_at'] + 0.1):
                        execute_job(job_id, store, Settings())
            self.assertEqual(store.get(job_id)['status'], 'FAILED')
            self.assertEqual(runner.await_count, expected_attempts)
            self.assertEqual(store.client.zcard(store.outbox), 0)

    def test_existing_four_stage_pipeline_runs_inside_worker_and_keeps_music(self):
        settings = Settings(openai_api_key='test', kakao_rest_api_key='test')
        store = new_store()
        body = request(None, 'CAR')
        doc, _ = store.reserve('ITINERARY_GENERATION', body.model_dump(mode='json'),
                                {'generation_job_id': 1}, 'req-worker', 'trip')
        with patch('ai_service.jobs.worker.KakaoPlaces', return_value=PlaceClient()), \
             patch('ai_service.jobs.worker.OpenAIPlanner', return_value=Planner()), \
             patch.object(store, 'progress', wraps=store.progress) as progress:
            execute_job(doc['job_id'], store, settings)
        result = store.get(doc['job_id'])
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertEqual([call.args[2:] for call in progress.call_args_list], [
            (stage, state) for stage in ('PLACE_RECOMMEND', 'STAY_RECOMMEND', 'ROUTE_OPTIMIZE', 'MUSIC_RECOMMEND')
            for state in ('STARTED', 'COMPLETED')])
        self.assertEqual(result['result']['music']['title'], '테스트 음악')
        self.assertEqual(result['result']['days'][0]['items'][-1]['place_type'], 'ACCOMMODATION')

    def test_timeout_is_retried_and_sensitive_exception_is_not_exposed(self):
        import asyncio
        async def slow(*args):
            await asyncio.sleep(1)
        store = new_store()
        job_id = reserve(store)['job_id']
        with patch('ai_service.jobs.worker.run_pipeline', slow):
            execute_job(job_id, store, Settings(generation_timeout_seconds=0.01))
        self.assertEqual(store.get(job_id)['error']['code'], 'JOB_TIMEOUT')
        self.assertEqual(store.get(job_id)['status'], 'RETRYING')
        job_id = reserve(store, 'secret')['job_id']
        with patch('ai_service.jobs.worker.run_pipeline', AsyncMock(side_effect=RuntimeError('secret API token'))):
            execute_job(job_id, store, Settings())
        self.assertNotIn('secret API token', json.dumps(store.public(store.get(job_id)).model_dump(mode='json')))
