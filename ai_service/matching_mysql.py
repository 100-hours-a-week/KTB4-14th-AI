"""Read-only MySQL candidate source, mapped to the existing E5 input contract.

SQL is deployment-owned, never provided by an HTTP caller. Two parameterized
SELECTs describe the actual backend schema, which can differ by deployment.
No matching/profile tables or fabricated profiles are created by this service.
"""
import asyncio
import json
import logging
import re

import pymysql
from pydantic import ValidationError

from ai_service.errors import ServiceUnavailable
from ai_service.jobs.errors import JobApiError
from ai_service.jobs.schemas import MatchingDatabaseJobRequest, MatchingJobRequest, DatabaseMatchingRecommendationRequest

logger = logging.getLogger(__name__)


def require_mysql_settings(settings):
    if not (settings.matching_mysql_database and settings.matching_mysql_user
            and settings.matching_mysql_query_file):
        raise ServiceUnavailable(reason='matching_db_settings_missing')


def _load_queries(path):
    try:
        queries = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(queries, dict) or set(queries) != {'context', 'candidates'}:
            raise ValueError('Both query mappings are required')
        for query in queries.values():
            # Only one SELECT per mapping; transaction READ ONLY is the final guard.
            if not isinstance(query, str) or not re.match(r'^\s*SELECT\b', query, re.I):
                raise ValueError('Only SELECT mappings are supported')
            if ';' in query or re.search(r'\b(INTO|FOR\s+UPDATE|LOCK\s+IN\s+SHARE)\b', query, re.I):
                raise ValueError('Writes and locks are not supported')
        if not re.search(r'\bLIMIT\s+%\(candidate_limit\)s\s*$', queries['candidates'], re.I):
            raise ValueError('Candidate SELECT must end with the server-owned LIMIT')
        return queries
    except (OSError, ValueError, TypeError) as exc:
        raise ServiceUnavailable(reason='matching_db_query_mapping_missing') from exc


def _themes(row):
    value = row.get('themes')
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError('themes must be a JSON array')
    return value


class MySQLMatchingCandidateSource:
    def __init__(self, settings, *, connect=None):
        self.settings = settings
        self.connect = connect or pymysql.connect

    async def recommendation_request(self, request: MatchingDatabaseJobRequest) -> MatchingJobRequest:
        return await asyncio.to_thread(self._read, request)

    def _read(self, request):
        require_mysql_settings(self.settings)
        queries = _load_queries(self.settings.matching_mysql_query_file)
        # Server-owned page size. Keyset mappings rank every eligible user;
        # legacy mappings detect overflow instead of silently truncating.
        params = dict(matching_request_id=request.matching_request_id,
                      travel_plan_id=request.travel_plan_id, requester_id=request.requester_id,
                      candidate_limit=201, after_user_id=0)
        options = dict(host=self.settings.matching_mysql_host, port=self.settings.matching_mysql_port,
                       user=self.settings.matching_mysql_user, password=self.settings.matching_mysql_password,
                       database=self.settings.matching_mysql_database, charset='utf8mb4',
                       connect_timeout=3, read_timeout=10, write_timeout=10, autocommit=False,
                       cursorclass=pymysql.cursors.DictCursor)
        if self.settings.matching_mysql_socket:
            options['unix_socket'] = self.settings.matching_mysql_socket
        if self.settings.matching_mysql_ssl_ca:
            options.update(ssl_ca=self.settings.matching_mysql_ssl_ca,
                           ssl_verify_cert=True, ssl_verify_identity=True)
        try:
            with self.connect(**options) as connection:
                try:
                    with connection.cursor() as cursor:
                        # Both reads share one snapshot. No result persistence in MySQL.
                        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                        cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                        cursor.execute(queries['context'], params)
                        contexts = cursor.fetchmany(2)
                        if not contexts:
                            raise JobApiError(404, 'MATCHING_REQUEST_NOT_FOUND', '매칭 요청을 찾을 수 없습니다.')
                        if len(contexts) != 1:
                            raise ValueError('Context must be unique')
                        context = contexts[0]
                        for name in ('matching_request_id', 'requester_id'):
                            if context.get(name) != params[name]:
                                raise JobApiError(403, 'MATCHING_REQUEST_FORBIDDEN', '매칭 요청의 소유자가 일치하지 않습니다.')
                        # A legacy mapping may bind a travel ID. Current backend
                        # matching has no such column or inferred trip selection.
                        if 'travel_plan_id' in context and context['travel_plan_id'] != request.travel_plan_id:
                            raise JobApiError(403, 'MATCHING_REQUEST_FORBIDDEN', '매칭 요청의 여행 식별자가 일치하지 않습니다.')
                        if 'travel_plan_id' not in context and request.travel_plan_id is not None:
                            raise JobApiError(400, 'INVALID_REQUEST', '현재 매칭 DB 요청에는 travel_plan_id를 넣지 마세요.')
                        cursor.execute(queries['candidates'], params)
                        rows = cursor.fetchmany(201)
                        paged = '%(after_user_id)s' in queries['candidates']
                        if paged:
                            # Keyset pages stay in the same snapshot. Never truncate
                            # to the first 200 users before computing the global top K.
                            while len(rows) and len(rows) % 201 == 0:
                                last = rows[-1]['user_id']
                                if not isinstance(last, int) or last <= params['after_user_id']:
                                    raise ValueError('Candidate page must advance by user ID')
                                params['after_user_id'] = last
                                cursor.execute(queries['candidates'], params)
                                page = cursor.fetchmany(201)
                                if any(row['user_id'] <= last for row in page):
                                    raise ValueError('Candidate keyset page did not advance')
                                rows.extend(page)
                                if len(rows) > 10000:
                                    raise JobApiError(422, 'MATCH_CANDIDATE_LIMIT_EXCEEDED', '후보가 10,000명을 초과했습니다. 조회 범위를 좁혀 주세요.')
                                if len(page) < 201:
                                    break
                        elif len(rows) > 200:
                            raise JobApiError(422, 'MATCH_CANDIDATE_LIMIT_EXCEEDED',
                                              '후보가 200명을 초과했습니다. 조회 범위를 좁혀 주세요.')
                        candidates = [dict(user_id=row['user_id'], themes=_themes(row), pace=row['pace'],
                                           gender=row.get('gender'), budget_min=row.get('budget_min'),
                                           budget_max=row.get('budget_max'), introduction=row.get('introduction') or '')
                                      for row in rows]
                        budget_available = context.get('candidate_budget_available', True)
                        if budget_available not in (True, False, 0, 1):
                            raise ValueError('Invalid candidate budget availability')
                        if not budget_available:
                            from ai_service.matching_catalog import THEMES, PACES, GENDERS, PREFERRED_GENDERS
                            if (context['preferred_companion_gender'] not in PREFERRED_GENDERS
                                or context['pace'] not in PACES or not 1 <= len(_themes(context)) <= 3
                                or not set(_themes(context)) <= set(THEMES)):
                                raise ValueError('Invalid backend matching condition')
                            if any(row['gender'] not in GENDERS or row['pace'] not in PACES
                                   or not 1 <= len(_themes(row)) <= 4 or not set(_themes(row)) <= set(THEMES)
                                   for row in rows):
                                raise ValueError('Invalid backend matching profile')
                        result = (DatabaseMatchingRecommendationRequest if paged or not budget_available else MatchingJobRequest)(
                            **{name: params[name] for name in ('matching_request_id', 'travel_plan_id', 'requester_id')},
                            preferred_companion_gender=context['preferred_companion_gender'],
                            themes=_themes(context), pace=context['pace'],
                            budget_min=context.get('budget_min'), budget_max=context.get('budget_max'),
                            candidates=candidates, limit=request.top_k,
                            **({'candidate_budget_available':bool(budget_available)} if paged or not budget_available else {}),
                        )
                finally:
                    connection.rollback()
        except pymysql.MySQLError as exc:
            code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            # MySQL exception text can contain credentials, SQL and profile data.
            logger.warning('matching_db_failed mysql_error_code=%s', code)
            reason = ('matching_db_authentication_failed' if code in {1044, 1045, 1142, 1143, 1698}
                      else 'matching_db_schema_missing' if code in {1049, 1054, 1064, 1146}
                      else 'matching_db_query_failed')
            raise ServiceUnavailable(reason=reason) from exc
        except (KeyError, ValueError, TypeError, ValidationError) as exc:
            raise ServiceUnavailable(reason='matching_db_data_invalid') from exc
        logger.info('matching_db_candidates_loaded candidate_count=%s', len(result.candidates))
        return result
