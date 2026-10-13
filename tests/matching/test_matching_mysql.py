"""MySQL mapping, identifiers, safe errors and the queue-to-E5 connection."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
import pymysql

from ai_service.core.config import Settings
from ai_service.core.errors import ServiceUnavailable
from ai_service.jobs.service import (
    JobService
)
MATCHING_DB_EXAMPLE = {"matching_request_id":201,"travel_plan_id":101,"requester_id":12,"limit":10}
from ai_service.jobs.errors import JobApiError
from ai_service.jobs.schemas import MatchingDatabaseJobRequest, MatchingJobRequest
from ai_service.jobs.worker import execute_job, job_error
from ai_service.main import create_app
from ai_service.matching.service import eligible_candidates
from ai_service.matching.mysql import MySQLMatchingCandidateSource
from tests.jobs.test_jobs import new_store

HEADERS = {'Authorization': 'Bearer test-only'}
CONTEXT = {**MATCHING_DB_EXAMPLE, 'preferred_companion_gender': 'ANY', 'pace': 'BALANCED',
           'themes': '["자연", "미식"]', 'budget_min': 300000, 'budget_max': 800000}
CANDIDATE = {'user_id': 35, 'pace': 'BALANCED', 'themes': '["자연", "사진"]',
             'budget_min': 300000, 'budget_max': 800000, 'introduction': '자연을 좋아해요'}


class FakeCursor:
    def __init__(self, context=None, candidates=None):
        self.context = [CONTEXT] if context is None else context
        self.candidates = [CANDIDATE] if candidates is None else candidates
        self.calls = []
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, *args):
        self.calls.append(args)

    def fetchmany(self, size):
        self.reads += 1
        return (self.context if self.reads == 1 else self.candidates)[:size]


class FakeConnection:
    def __init__(self, cursor):
        self.reader = cursor
        self.rollback = Mock()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self.reader


class MySQLMatchingTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mapping = Path(self.directory.name) / 'queries.json'
        self.queries = {'context': 'SELECT * FROM context WHERE matching_request_id = %(matching_request_id)s',
                        'candidates': 'SELECT * FROM candidates WHERE user_id <> %(requester_id)s LIMIT %(candidate_limit)s'}
        self.mapping.write_text(json.dumps(self.queries))
        self.settings = Settings(api_token='test-only', matching_mysql_database='test',
                                 matching_mysql_user='reader', matching_mysql_password='secret-test-only',
                                 matching_mysql_query_file=self.mapping)
        self.request = MatchingDatabaseJobRequest.model_validate(MATCHING_DB_EXAMPLE)

    def source(self, **rows):
        cursor = FakeCursor(**rows)
        connection = FakeConnection(cursor)
        connector = Mock(return_value=connection)
        return MySQLMatchingCandidateSource(self.settings, connect=connector), cursor, connection, connector

    def test_binds_ids_and_reads_same_read_only_snapshot(self):
        source, cursor, connection, connector = self.source()
        result = asyncio.run(source.recommendation_request(self.request))
        self.assertEqual(result.requester_id, 12)
        self.assertEqual(result.candidates[0].theme, ['자연', '사진'])
        self.assertIsNone(result.candidates[0].gender)
        self.assertEqual([candidate.user_id for candidate in eligible_candidates(result)], [35])
        self.assertEqual(cursor.calls[:2], [('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ',),
                                          ('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY',)])
        self.assertEqual(cursor.calls[2][1]['matching_request_id'], 201)
        self.assertEqual(cursor.calls[3][1]['candidate_limit'], 201)
        connection.rollback.assert_called_once()
        self.assertEqual(connector.call_args.kwargs['connect_timeout'], 3)
        self.assertNotIn('secret-test-only', repr(self.settings))

    def test_no_context_or_wrong_owner_fail_and_rollback(self):
        for contexts, code in [([], 'MATCHING_REQUEST_NOT_FOUND'),
                               ([{**CONTEXT, 'requester_id': 99}], 'MATCHING_REQUEST_FORBIDDEN')]:
            source, cursor, connection, _ = self.source(context=contexts)
            with self.assertRaises(JobApiError) as caught:
                asyncio.run(source.recommendation_request(self.request))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(cursor.reads, 1)
            connection.rollback.assert_called_once()

    def test_malformed_profiles_duplicate_users_and_ambiguous_context_fail_closed(self):
        cases = [dict(candidates=[{**CANDIDATE, 'themes': 'not-json'}]),
                 dict(candidates=[{**CANDIDATE, 'pace': 'INVALID'}]),
                 dict(candidates=[CANDIDATE, CANDIDATE]), dict(context=[CONTEXT, CONTEXT])]
        for rows in cases:
            source, _, connection, _ = self.source(**rows)
            with self.assertRaises(ServiceUnavailable) as caught:
                asyncio.run(source.recommendation_request(self.request))
            self.assertEqual(caught.exception.reason, 'matching_db_data_invalid')
            self.assertFalse(job_error(caught.exception)['retryable'])
            connection.rollback.assert_called_once()

    def test_candidate_overflow_is_not_arbitrarily_truncated(self):
        source, _, connection, _ = self.source(candidates=[{**CANDIDATE, 'user_id': n} for n in range(1, 202)])
        with self.assertRaises(JobApiError) as caught:
            asyncio.run(source.recommendation_request(self.request))
        self.assertEqual(caught.exception.code, 'MATCH_CANDIDATE_LIMIT_EXCEEDED')
        connection.rollback.assert_called_once()

    def test_bad_query_mapping_never_connects(self):
        for sql in ('DELETE FROM users', 'SELECT 1; DROP TABLE users', 'SELECT value INTO OUTFILE \'/tmp/x\'',
                    'SELECT * FROM candidates', 'SELECT * FROM candidates LIMIT 200'):
            self.mapping.write_text(json.dumps({**self.queries, 'candidates': sql}))
            connector = Mock()
            with self.assertRaises(ServiceUnavailable) as caught:
                asyncio.run(MySQLMatchingCandidateSource(self.settings, connect=connector).recommendation_request(self.request))
            self.assertEqual(caught.exception.reason, 'matching_db_query_mapping_missing')
            connector.assert_not_called()

    def test_db_errors_are_sanitized_and_only_transient_errors_retry(self):
        for code, retryable in ((1045, False), (1146, False), (1054, False), (2003, True), (2013, True)):
            connector = Mock(side_effect=pymysql.OperationalError(code, 'secret-test-only sensitive SQL'))
            with self.assertLogs('ai_service.matching.mysql', level='WARNING') as logs:
                with self.assertRaises(ServiceUnavailable) as caught:
                    asyncio.run(MySQLMatchingCandidateSource(self.settings, connect=connector).recommendation_request(self.request))
            error = job_error(caught.exception)
            self.assertEqual(error['retryable'], retryable)
            self.assertNotIn('secret-test-only', str(error) + str(logs.output))

    def test_socket_and_tls_settings_are_passed_without_logging_password(self):
        source, _, _, connector = self.source()
        source.settings = replace(self.settings, matching_mysql_socket='/tmp/mysql.sock', matching_mysql_ssl_ca='/ca.pem')
        asyncio.run(source.recommendation_request(self.request))
        self.assertEqual(connector.call_args.kwargs['unix_socket'], '/tmp/mysql.sock')
        self.assertTrue(connector.call_args.kwargs['ssl_verify_identity'])

    def test_http_accepts_ids_without_candidates_and_worker_reads_db_before_e5(self):
        source, _, _, _ = self.source()
        resolved = asyncio.run(source.recommendation_request(self.request))
        store = new_store()
        app = create_app(settings=self.settings)
        with TestClient(app) as client:
            app.state.jobs = JobService(store, lambda doc: None)
            accepted = client.post('/api/ai/v1/matches/candidates', json=MATCHING_DB_EXAMPLE, headers=HEADERS)
            self.assertEqual(accepted.status_code, 202, accepted.text)
            job_id = accepted.json()['job_id']
            self.assertNotIn('candidates', store.get(job_id)['payload'])
            lookup = Mock(recommendation_request=AsyncMock(return_value=resolved))
            recommender = Mock(encoder=object(), recommend=AsyncMock(return_value=Mock(data=Mock(
                model_dump=Mock(return_value={'requester_id': 12, 'recommendations': [{'user_id': 35, 'score': 0.8, 'rank': 1}]})))))
            with patch('ai_service.jobs.worker.MySQLMatchingCandidateSource', return_value=lookup), \
                 patch('ai_service.jobs.worker._MATCHING_ENCODER', None), \
                 patch('ai_service.jobs.worker.E5MatchingRecommender', return_value=recommender), \
                 patch.object(store, 'progress', wraps=store.progress) as progress:
                execute_job(job_id, store, self.settings)
            lookup.recommendation_request.assert_awaited_once_with(self.request)
            recommender.recommend.assert_awaited_once_with(resolved)
            self.assertEqual([call.args[2:] for call in progress.call_args_list],
                             [('CANDIDATE_LOOKUP', 'STARTED'), ('CANDIDATE_LOOKUP', 'COMPLETED'),
                              ('TRAVELER_MATCHING', 'STARTED')])
            status = client.get(accepted.json()['status_url'], headers=HEADERS).json()
            self.assertEqual(status['status'], 'COMPLETED')
            self.assertEqual(status['result']['recommendations'][0]['user_id'], 35)

    def test_missing_configuration_rejects_ids_only_http_without_mock_fallback(self):
        with TestClient(create_app(settings=Settings(api_token='test-only'))) as client:
            response = client.post('/api/ai/v1/matches/candidates', json=MATCHING_DB_EXAMPLE, headers=HEADERS)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('recommendations', response.text)

    def test_worker_does_not_infer_if_db_has_no_eligible_candidates(self):
        source, _, _, _ = self.source(candidates=[])
        resolved = asyncio.run(source.recommendation_request(self.request))
        store = new_store()
        doc, _ = store.reserve('TRAVELER_MATCHING', MATCHING_DB_EXAMPLE,
                              {'matching_request_id': 201, 'travel_plan_id': 101}, 'req-db', 'db-empty')
        lookup = Mock(recommendation_request=AsyncMock(return_value=resolved))
        with patch('ai_service.jobs.worker.MySQLMatchingCandidateSource', return_value=lookup), \
             patch('ai_service.jobs.worker.E5MatchingRecommender') as model:
            execute_job(doc['job_id'], store, self.settings)
        status = store.get(doc['job_id'])
        self.assertEqual(status['status'], 'FAILED')
        self.assertEqual(status['error']['code'], 'NO_MATCH_CANDIDATES')
        self.assertEqual(status['attempt'], 1)
        model.assert_not_called()
