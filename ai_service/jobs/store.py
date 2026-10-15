"""Redis 작업 기록과 발행·복구용 발행 대기 목록을 원자적으로 관리한다.

실행 권한을 보유한 워커만 작업 상태를 갱신할 수 있다. 권한이 만료된 작업은 다시 전달한다.
HTTP 202를 반환하기 전에 작업과 발행 대기 항목을 함께 저장한다.
"""
import hashlib
import json
import time
from uuid import uuid4

import redis

from ai_service.jobs.errors import JobApiError
from ai_service.schemas.jobs import JobStatus

PREFIX = 'audigo:jobs:'
RESERVE = """
local prior = redis.call('GET', KEYS[1])
if prior then
    local saved = redis.call('GET', ARGV[4] .. prior)
    if saved then
        if cjson.decode(saved).fingerprint ~= ARGV[3] then return {'CONFLICT'} end
        return {'EXISTING', saved}
    end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[5])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[5])
redis.call('ZADD', KEYS[3], ARGV[6], ARGV[1])
return {'NEW', ARGV[2]}
"""
MUTATE = """
local raw = redis.call('GET', KEYS[1])
if not raw then redis.call('ZREM', KEYS[2], ARGV[2]); return nil end
local d = cjson.decode(raw)
local action, id, now, owner = ARGV[1], ARGV[2], tonumber(ARGV[3]), ARGV[4]
if action == 'claim' then
    if d.status == 'COMPLETED' or d.status == 'FAILED' then return nil end
    if d.status == 'RUNNING' and d.lease_until > now then return nil end
    if d.next_attempt_at > now then return nil end
    if d.attempt >= d.max_attempts then
        d.status = 'FAILED'
        d.stage_status, d.next_retry_at, d.lease_until, d.lease_token = 'FAILED', cjson.null, 0, ''
        d.error = '{"code":"WORKER_ATTEMPTS_EXHAUSTED","message":"작업 실행 횟수를 초과했습니다.","retryable":true,"details":[]}'
        d.updated_at = now
        redis.call('ZREM', KEYS[2], id)
    else
        d.attempt = d.attempt + 1
        d.status, d.lease_token, d.lease_until = 'RUNNING', owner, now + tonumber(ARGV[5])
        d.updated_at, d.error, d.next_retry_at = now, cjson.null, cjson.null
        redis.call('ZADD', KEYS[2], d.lease_until, id)
    end
elseif action == 'dispatch' then
    local score = redis.call('ZSCORE', KEYS[2], id)
    if score and tonumber(score) == tonumber(ARGV[5]) then
        -- Publication is confirmed. The durable broker owns queued delivery;
        -- a worker claim creates a fresh lease/recovery entry. Never remove it.
        redis.call('ZREM', KEYS[2], id)
    end
    return nil
else
    if d.status ~= 'RUNNING' or d.lease_token ~= owner or d.lease_until <= now then return nil end
    if action == 'progress' then
        d.current_stage, d.stage_status = ARGV[5], ARGV[6]
    else
        d.status = ARGV[5]
        d.result = ARGV[6] == 'null' and cjson.null or ARGV[6]
        d.error = ARGV[7] == 'null' and cjson.null or ARGV[7]
        d.lease_until, d.lease_token = 0, ''
        if d.status == 'RETRYING' then
            d.next_attempt_at, d.next_retry_at = tonumber(ARGV[8]), tonumber(ARGV[8])
            redis.call('ZADD', KEYS[2], d.next_attempt_at, id)
        else
            d.next_retry_at = cjson.null
            redis.call('ZREM', KEYS[2], id)
            if d.status == 'COMPLETED' then d.current_stage, d.stage_status = 'COMPLETE', 'COMPLETED' end
            if d.status == 'FAILED' then d.stage_status = 'FAILED' end
        end
    end
    d.updated_at = now
end
local updated = cjson.encode(d)
redis.call('SET', KEYS[1], updated, 'KEEPTTL')
return updated
"""


class RedisJobStore:
    def __init__(self, client, *, ttl_seconds=86400, max_attempts=3, lease_seconds=360):
        self.client = client
        self.ttl, self.max_attempts, self.lease = ttl_seconds, max_attempts, lease_seconds
        self.outbox = PREFIX + 'outbox'

    @classmethod
    def from_settings(cls, settings):
        if not settings.jobs_redis_url:
            raise JobApiError(503, 'QUEUE_UNAVAILABLE', '작업 큐 설정이 없습니다.', retryable=True)
        return cls(redis.Redis.from_url(settings.jobs_redis_url, decode_responses=True,
                                        socket_connect_timeout=3, socket_timeout=3),
                   ttl_seconds=settings.job_ttl_seconds,
                   max_attempts=settings.job_max_retries + 1,
                   lease_seconds=settings.generation_timeout_seconds + 60)

    def key(self, job_id):
        return PREFIX + 'record:' + job_id

    def reserve(self, job_type, payload, identity, request_id, idempotency_key):
        if job_type != 'ITINERARY_GENERATION':
            raise JobApiError(400, 'UNSUPPORTED_JOB_TYPE', '지원하지 않는 작업 유형입니다.')
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        dedup = hashlib.sha256(f'{job_type}:{idempotency_key}'.encode()).hexdigest()
        now = time.time()
        job_id = 'job_itinerary_' + uuid4().hex
        # Keep nested input/result JSON opaque to Lua: cjson would turn [] into {}.
        doc = dict(job_id=job_id, job_type=job_type, status='QUEUED', payload=serialized,
                   fingerprint=fingerprint, request_id=request_id, attempt=0, max_attempts=self.max_attempts,
                   created_at=now, updated_at=now, expires_at=now + self.ttl,
                   lease_token='', lease_until=0, next_attempt_at=now, next_retry_at=None,
                   current_stage=None, stage_status=None, result=None, error=None,
                   **{key: str(value) for key, value in identity.items()})
        result = self.client.eval(RESERVE, 3, PREFIX + 'dedup:' + dedup, self.key(job_id), self.outbox,
                                  job_id, json.dumps(doc, ensure_ascii=False), fingerprint,
                                  PREFIX + 'record:', self.ttl, now)
        if result[0] == 'CONFLICT':
            raise JobApiError(409, 'IDEMPOTENCY_CONFLICT', '같은 중복 방지 키에 다른 요청이 전달됐습니다.')
        return self._decode(result[1]), result[0] == 'NEW'

    @staticmethod
    def _decode(raw):
        doc = json.loads(raw)
        for name in ('payload', 'result', 'error'):
            if isinstance(doc.get(name), str):
                doc[name] = json.loads(doc[name])
        # Redis Lua numbers cannot exactly represent all Java Long IDs.
        for name in ('generation_job_id', 'travel_plan_id'):
            if name in doc:
                doc[name] = int(doc[name])
        return doc

    def get(self, job_id):
        raw = self.client.get(self.key(job_id))
        return self._decode(raw) if raw else None

    def public(self, doc):
        values = {name: doc[name] for name in JobStatus.model_fields if name in doc}
        values['status_url'] = '/api/ai/v1/jobs/' + doc['job_id']
        return JobStatus.model_validate(values)

    def _mutate(self, job_id, action, owner='', *args):
        raw = self.client.eval(MUTATE, 2, self.key(job_id), self.outbox,
                               action, job_id, time.time(), owner, *args)
        return self._decode(raw) if raw else None

    def claim(self, job_id, owner):
        doc = self._mutate(job_id, 'claim', owner, self.lease)
        return doc if doc and doc['status'] == 'RUNNING' and doc['lease_token'] == owner else None

    def progress(self, job_id, owner, stage, status):
        return self._mutate(job_id, 'progress', owner, stage, status)

    def finish(self, job_id, owner, status, *, result=None, error=None, retry_at=0):
        return self._mutate(job_id, 'finish', owner, status,
                            json.dumps(result, ensure_ascii=False), json.dumps(error, ensure_ascii=False), retry_at)

    def due(self, limit=100):
        return self.client.zrangebyscore(self.outbox, '-inf', time.time(), start=0, num=limit, withscores=True)

    def dispatched(self, job_id, original_score):
        self._mutate(job_id, 'dispatch', '', original_score)

    def close(self):
        self.client.close()
