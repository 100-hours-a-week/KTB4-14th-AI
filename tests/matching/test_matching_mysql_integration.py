"""Disposable MySQL + real local E5; never connects to the user's server.

MYSQLD_BINARY=/usr/local/mysql/bin/mysqld enables this integration test.
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest

import pymysql

from ai_service.core.config import Settings
from ai_service.jobs.errors import JobApiError
from ai_service.jobs.schemas import MatchingDatabaseJobRequest
from ai_service.jobs.worker import execute_job
from ai_service.matching.mysql import MySQLMatchingCandidateSource
from ai_service.matching.service import eligible_candidates
from ai_service.matching.dev import DATA_DIR, make_dataset, insert_dataset
from tests.jobs.test_jobs import new_store

BINARY = os.environ.get('MYSQLD_BINARY')


@unittest.skipUnless(BINARY, 'Set MYSQLD_BINARY to start an isolated disposable MySQL')
class MySQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix='audigo-db-test-', dir='/private/tmp')
        cls.folder = Path(cls.temp.name)
        cls.process = None
        try:
            base = Path(BINARY).resolve().parents[1]
            args = [BINARY, '--no-defaults', '--basedir=' + str(base), '--datadir=' + str(cls.folder / 'data')]
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                cls.port = sock.getsockname()[1]
            initialized = subprocess.run(args + ['--initialize-insecure'], capture_output=True, timeout=60)
            if initialized.returncode:
                raise RuntimeError('Disposable MySQL initialization failed')
            cls.process = subprocess.Popen(args + ['--bind-address=127.0.0.1', '--port=' + str(cls.port),
                '--socket=' + str(cls.folder / 'mysql.sock'), '--pid-file=' + str(cls.folder / 'test.pid'),
                '--log-error=' + str(cls.folder / 'test.err'), '--mysqlx=OFF', '--skip-log-bin'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    connection = cls.connect()
                    break
                except pymysql.MySQLError:
                    if cls.process.poll() is not None:
                        raise RuntimeError('Disposable MySQL server stopped')
                    time.sleep(0.1)
            else:
                raise RuntimeError('Disposable MySQL startup timed out')
            with connection as conn, conn.cursor() as cursor:
                cursor.execute('CREATE DATABASE matching_test')
                cursor.execute('USE matching_test')
                cursor.execute('CREATE TABLE matching_context (matching_request_id BIGINT PRIMARY KEY, travel_plan_id BIGINT, requester_id BIGINT, preferred_companion_gender VARCHAR(20), pace VARCHAR(20), themes JSON, budget_min INT, budget_max INT)')
                cursor.execute('CREATE TABLE matching_candidates (matching_request_id BIGINT, user_id BIGINT, gender VARCHAR(20), pace VARCHAR(20), themes JSON, budget_min INT, budget_max INT, introduction VARCHAR(1000), PRIMARY KEY(matching_request_id,user_id))')
                cursor.execute('INSERT INTO matching_context VALUES (201,101,12,\'ANY\',\'BALANCED\',%s,300000,800000)',
                               (json.dumps(['자연', '미식']),))
                cursor.executemany('INSERT INTO matching_candidates VALUES (201,%s,%s,%s,%s,%s,%s,%s)', [
                    (35, None, 'BALANCED', json.dumps(['자연', '미식']), 300000, 800000, '자연과 맛집을 좋아해요'),
                    (36, 'Male', 'PACKED', json.dumps(['문화', '도심']), 300000, 800000, '도심 관광을 좋아해요'),
                    (12, 'Female', 'BALANCED', json.dumps(['자연']), 300000, 800000, '요청자 본인'),
                ])
            mapping = cls.folder / 'queries.json'
            mapping.write_text(json.dumps({
                'context': 'SELECT * FROM matching_context WHERE matching_request_id=%(matching_request_id)s AND travel_plan_id=%(travel_plan_id)s AND requester_id=%(requester_id)s',
                'candidates': 'SELECT * FROM matching_candidates WHERE matching_request_id=%(matching_request_id)s AND user_id<>%(requester_id)s ORDER BY user_id LIMIT %(candidate_limit)s',
            }))
            cls.settings = Settings(matching_mysql_port=cls.port, matching_mysql_database='matching_test',
                matching_mysql_user='root', matching_mysql_password='', matching_mysql_query_file=mapping)
            cls.dataset, cls.fingerprint = make_dataset()
            cls.create_dev_db('matching_dev_test', cls.dataset)
            with cls.connect() as conn, conn.cursor() as cursor:
                cursor.execute("CREATE USER 'test_reader'@'localhost' IDENTIFIED BY 'integration-test-only'")
                for table in ['regions','users','matching_profiles','matching_profile_themes',
                              'matching_requests','matching_requests_themes']:
                    cursor.execute(f"GRANT SELECT ON matching_dev_test.{table} TO 'test_reader'@'localhost'")
            cls.dev_settings = replace(cls.settings,matching_mysql_database='matching_dev_test',
                matching_mysql_user='test_reader',matching_mysql_password='integration-test-only',
                matching_mysql_query_file=DATA_DIR/'matching_dev_queries.json',
                matching_e5_weight=.85,matching_theme_weight=.10,matching_pace_weight=.05)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def connect(cls):
        return pymysql.connect(host='127.0.0.1', port=cls.port, user='root', password='',
                               autocommit=True, connect_timeout=1)

    @classmethod
    def create_dev_db(cls, name, dataset):
        with cls.connect() as conn, conn.cursor() as cursor:
            cursor.execute('CREATE DATABASE '+name)
            cursor.execute('USE '+name)
            schema = '\n'.join(line for line in (DATA_DIR/'matching_dev_schema.sql').read_text().splitlines()
                               if not line.lstrip().startswith('--'))
            for sql in schema.split(';'):
                if sql.strip(): cursor.execute(sql)
            insert_dataset(cursor,dataset,cls.fingerprint)

    @classmethod
    def tearDownClass(cls):
        if cls.process is not None and cls.process.poll() is None:
            cls.process.terminate()
            cls.process.wait(timeout=30)
        cls.temp.cleanup()

    def test_real_database_ids_to_worker_and_real_e5_result(self):
        payload = dict(matching_request_id=201, travel_plan_id=101, requester_id=12, limit=10)
        store = new_store()
        doc, _ = store.reserve('TRAVELER_MATCHING', payload,
            {'matching_request_id': 201, 'travel_plan_id': 101}, 'req-mysql-integration', 'mysql-integration')
        execute_job(doc['job_id'], store, self.settings)
        saved = store.get(doc['job_id'])
        self.assertEqual(saved['status'], 'COMPLETED', saved['error'])
        result = saved['result']
        self.assertEqual((result['matching_request_id'], result['requester_id']), (201, 12))
        self.assertEqual({candidate['user_id'] for candidate in result['recommendations']}, {35, 36})
        self.assertEqual([candidate['rank'] for candidate in result['recommendations']], [1, 2])
        scores = [candidate['score'] for candidate in result['recommendations']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # Only controlled fixtures exist; no result rows are written back to DB.
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM matching_test.matching_candidates')
            self.assertEqual(cursor.fetchone()[0], 3)

    def test_actual_query_filters_wrong_owner_and_reports_unknown_request(self):
        request = MatchingDatabaseJobRequest(matching_request_id=201, travel_plan_id=101, requester_id=99)
        with self.assertRaises(JobApiError) as caught:
            asyncio.run(MySQLMatchingCandidateSource(self.settings).recommendation_request(request))
        self.assertEqual(caught.exception.code, 'MATCHING_REQUEST_NOT_FOUND')

    def test_actual_mysql_enforces_read_only_transaction(self):
        # Issue a write immediately after the source's own START TRANSACTION,
        # using a test-only connection wrapper. MySQL must reject it (1792).
        real_connect = pymysql.connect
        class Reader:
            def __init__(self, cursor):
                self.cursor = cursor
            def __enter__(self):
                self.cursor.__enter__()
                return self
            def __exit__(self, *args):
                return self.cursor.__exit__(*args)
            def execute(self, sql, *args):
                value = self.cursor.execute(sql, *args)
                if sql.startswith('START TRANSACTION'):
                    try:
                        self.cursor.execute('DELETE FROM matching_candidates')
                    except pymysql.MySQLError as exc:
                        if exc.args[0] != 1792:
                            raise
                    else:
                        raise AssertionError('Source transaction permitted a write')
                return value
            def fetchmany(self, count):
                return self.cursor.fetchmany(count)
        class Connection:
            def __init__(self, conn):
                self.conn = conn
            def __enter__(self):
                self.conn.__enter__()
                return self
            def __exit__(self, *args):
                return self.conn.__exit__(*args)
            def cursor(self):
                return Reader(self.conn.cursor())
            def rollback(self):
                return self.conn.rollback()
        source = MySQLMatchingCandidateSource(self.settings, connect=lambda **kwargs: Connection(real_connect(**kwargs)))
        request = MatchingDatabaseJobRequest(matching_request_id=201, travel_plan_id=101, requester_id=12)
        result = asyncio.run(source.recommendation_request(request))
        self.assertEqual(len(result.candidates), 2)

    def test_development_sql_all_1000_requests_matches_independent_eligibility_labels(self):
        expected={}
        for rid,uid,*_ in self.dataset['labels']:
            expected.setdefault(rid,set()).add(uid)
        async def check():
            source=MySQLMatchingCandidateSource(self.dev_settings)
            for row in self.dataset['requests']:
                lookup=MatchingDatabaseJobRequest(matching_request_id=row['id'],
                    requester_id=row['requester_id'])
                result=await source.recommendation_request(lookup)
                self.assertEqual({c.user_id for c in eligible_candidates(result)},expected.get(row['id'],set()),row['id'])
        asyncio.run(check())

    def test_reader_has_no_write_or_labels_and_schema_enforces_backend_enum(self):
        settings=self.dev_settings
        with pymysql.connect(host='127.0.0.1',port=self.port,user=settings.matching_mysql_user,
            password=settings.matching_mysql_password,database=settings.matching_mysql_database) as conn,conn.cursor() as cursor:
            for sql in ('SELECT * FROM matching_evaluation_labels', 'UPDATE users SET status=\'INACTIVE\' WHERE id=-1'):
                with self.assertRaises(pymysql.MySQLError) as caught:
                    cursor.execute(sql)
                self.assertEqual(caught.exception.args[0],1142)
        with self.connect() as conn,conn.cursor() as cursor:
            with self.assertRaises(pymysql.MySQLError) as caught:
                cursor.execute("UPDATE matching_dev_test.matching_profiles SET gender='OTHER' WHERE id=35")
            self.assertEqual(caught.exception.args[0],3819)

    def test_setup_script_is_idempotent_and_preserves_credentials(self):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch
        from dotenv import dotenv_values
        from scripts import setup_matching_dev_db as setup_script
        with TemporaryDirectory(prefix='audigo-setup-env-') as folder:
            root=Path(folder)
            private=root/'.env.matching-dev'
            with patch.object(setup_script,'ROOT',root),patch.object(setup_script,'LOCAL_ENV',private),redirect_stdout(io.StringIO()):
                setup_script.setup('127.0.0.1',self.port,'root','','audigo_matching_test_setup')
                first=dotenv_values(private)
                setup_script.setup('127.0.0.1',self.port,'root','','audigo_matching_test_setup')
                self.assertEqual(first,dotenv_values(private))
                self.assertEqual(private.stat().st_mode & 0o777,0o600)
                self.assertFalse(json.loads((root/'artifacts/matching/dataset-summary.json').read_text())['created'])
                with self.assertRaises(ValueError):
                    setup_script.setup('127.0.0.1',self.port,'root','','production')
            with self.connect() as conn,conn.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM audigo_matching_test_setup.users')
                self.assertEqual(cursor.fetchone()[0],1000)

    def test_keyset_pages_cover_all_users_and_theme_join_does_not_duplicate_profiles(self):
        from copy import deepcopy
        dataset=deepcopy(self.dataset)
        for p in dataset['profiles']:
            p.update(active=True,gender='FEMALE',themes=['NATURE','FOOD'])
        self.create_dev_db('matching_page_test',dataset)
        settings=replace(self.dev_settings,matching_mysql_database='matching_page_test',
                         matching_mysql_user='root',matching_mysql_password='')
        lookup=MatchingDatabaseJobRequest(matching_request_id=201,requester_id=201)
        result=asyncio.run(MySQLMatchingCandidateSource(settings).recommendation_request(lookup))
        self.assertEqual(len(result.candidates),999)
        self.assertEqual([c.user_id for c in result.candidates],[uid for uid in range(1,1001) if uid!=201])
        self.assertTrue(all(set(c.theme)=={'nature','food'} for c in result.candidates))

    def test_backend_incomplete_profiles_gender_and_unavailable_budget(self):
        self.create_dev_db('matching_boundary_test',self.dataset)
        with self.connect() as conn,conn.cursor() as cursor:
            cursor.execute('USE matching_boundary_test')
            cursor.execute('UPDATE matching_profiles SET is_active=FALSE')
            cursor.execute("UPDATE matching_profiles SET is_active=TRUE,gender='FEMALE' WHERE user_id BETWEEN 35 AND 38")
            cursor.execute("UPDATE matching_profiles SET gender='MALE' WHERE user_id=36")
            cursor.execute('UPDATE matching_profiles SET gender=NULL WHERE user_id=37')
            cursor.execute('DELETE FROM matching_profile_themes WHERE matching_profile_id=38')
            cursor.execute("UPDATE matching_requests SET preferred_companion_gender='ANY',budget_min=300000,budget_max=800000 WHERE id=201")
        settings=replace(self.dev_settings,matching_mysql_database='matching_boundary_test',
                         matching_mysql_user='root',matching_mysql_password='')
        lookup=MatchingDatabaseJobRequest(matching_request_id=201,requester_id=201)
        result=asyncio.run(MySQLMatchingCandidateSource(settings).recommendation_request(lookup))
        self.assertEqual([c.user_id for c in result.candidates],[35,36])
        self.assertFalse(result.candidate_budget_available)
        self.assertEqual([c.user_id for c in eligible_candidates(result)],[35,36])
        self.assertTrue(all(c.budget_min is None and not c.introduction for c in result.candidates))
        with self.connect() as conn,conn.cursor() as cursor:
            cursor.execute("UPDATE matching_boundary_test.matching_requests SET preferred_companion_gender='FEMALE' WHERE id=201")
        result=asyncio.run(MySQLMatchingCandidateSource(settings).recommendation_request(lookup))
        self.assertEqual([c.user_id for c in eligible_candidates(result)],[35])
        with self.assertRaises(JobApiError) as caught:
            asyncio.run(MySQLMatchingCandidateSource(settings).recommendation_request(
                MatchingDatabaseJobRequest(matching_request_id=201,requester_id=999)))
        self.assertEqual(caught.exception.code,'MATCHING_REQUEST_NOT_FOUND')

    @unittest.skipUnless(os.environ.get('REDIS_SERVER'), 'Set REDIS_SERVER for DB-to-Celery integration')
    def test_ids_only_http_to_real_redis_worker_mysql_e5_and_status(self):
        from celery.contrib.testing.worker import start_worker
        from fastapi.testclient import TestClient
        from ai_service.jobs.store import RedisJobStore
        from ai_service.jobs.worker import create_celery_app
        from ai_service.main import create_app
        from unittest.mock import patch
        with TemporaryDirectory(prefix='audigo-db-queue-') as folder:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                port=sock.getsockname()[1]
            process=subprocess.Popen([os.environ['REDIS_SERVER'],'--bind','127.0.0.1','--port',str(port),
                '--dir',folder,'--save',''],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            store=None
            try:
                settings=replace(self.dev_settings,api_token='test-only',
                    jobs_redis_url=f'redis://127.0.0.1:{port}/1',celery_broker_url=f'redis://127.0.0.1:{port}/0')
                store=RedisJobStore.from_settings(settings)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    try:
                        store.client.ping()
                        break
                    except Exception:
                        time.sleep(.05)
                else:
                    self.fail('Disposable Redis failed to start')
                celery=create_celery_app(settings)
                with patch('ai_service.jobs.worker._MATCHING_ENCODER',None), start_worker(celery,pool='solo',
                    perform_ping_check=False,queues=['audigo_control','audigo_matching'],shutdown_timeout=10), \
                     TestClient(create_app(settings=settings)) as client:
                    headers={'Authorization':'Bearer test-only'}
                    body=dict(matching_request_id=201,requester_id=201,limit=3)
                    accepted=client.post('/api/ai/v1/matches/candidates',json=body,headers=headers)
                    self.assertEqual(accepted.status_code,202,accepted.text)
                    deadline=time.monotonic()+30
                    while time.monotonic()<deadline:
                        status=client.get(accepted.json()['status_url'],headers=headers).json()
                        if status['status'] in {'COMPLETED','FAILED'}: break
                        time.sleep(.1)
                    self.assertEqual(status['status'],'COMPLETED',status)
                    self.assertEqual(len(status['result']['recommendations']),3)
                    positives={uid for rid,uid,grade,_ in self.dataset['labels'] if rid==201 and grade==2}
                    self.assertTrue(positives & {r['user_id'] for r in status['result']['recommendations']})
                    duplicate=client.post('/api/ai/v1/matches/candidates',json=body,headers=headers)
                    self.assertEqual(duplicate.json()['job_id'],accepted.json()['job_id'])
                    self.assertEqual(status['attempt'],1)
                    self.assertEqual(status['result']['unapplied_conditions'],['budget'])
            finally:
                if store: store.close()
                process.terminate()
                process.wait(timeout=10)
