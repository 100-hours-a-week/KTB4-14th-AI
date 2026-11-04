"""SQS 전용 실행 lease와 최종 결과 캐시. 선택 Redis 저장소는 큐나 사용자용 작업 상태를 관리하지 않는다."""
from collections import OrderedDict
from dataclasses import dataclass
import json
import threading
import time


class LeaseLost(RuntimeError):
    pass


class JobConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class Claim:
    state: str
    result: str | None = None


class MemoryJobStore:
    """단일 프로세스용. 재시작·서로 다른 프로세스 사이의 중복 방지는 보장하지 않는다."""

    def __init__(self, *, ttl=86400, lease_seconds=960, capacity=256):
        self.ttl, self.lease_seconds, self.capacity = ttl, lease_seconds, capacity
        self.records = OrderedDict()
        self.lock = threading.RLock()

    def acquire(self, job_id, fingerprint, owner):
        with self.lock:
            now = time.monotonic()
            for key, value in list(self.records.items()):
                if value["expires"] <= now:
                    del self.records[key]
            doc = self.records.get(job_id)
            if doc is None:
                if len(self.records) >= self.capacity:
                    victim = next((key for key, value in self.records.items()
                                   if value["result"] is not None or value["lease_until"] <= now), None)
                    if victim is None:
                        raise RuntimeError("SQS 중복 처리 캐시가 사용 중입니다.")
                    del self.records[victim]
                doc = dict(fingerprint=fingerprint, owner=None, lease_until=0,
                           expires=now + self.ttl, result=None)
                self.records[job_id] = doc
            if doc["fingerprint"] != fingerprint:
                raise JobConflict("동일 작업 ID에 다른 요청이 전달됐습니다.")
            if doc["result"] is not None:
                return Claim("CACHED", doc["result"])
            if doc["owner"] and doc["lease_until"] > now:
                return Claim("BUSY")
            doc.update(owner=owner, lease_until=now + self.lease_seconds, expires=now + self.ttl)
            return Claim("ACQUIRED")

    def _owned(self, job_id, owner):
        doc = self.records.get(job_id)
        if doc is None or doc["owner"] != owner or doc["lease_until"] <= time.monotonic():
            raise LeaseLost("SQS 실행 소유권이 만료됐습니다.")
        return doc

    def renew(self, job_id, owner):
        with self.lock:
            doc = self._owned(job_id, owner)
            doc.update(lease_until=time.monotonic() + self.lease_seconds,
                       expires=time.monotonic() + self.ttl)

    def finish(self, job_id, owner, result):
        with self.lock:
            doc = self._owned(job_id, owner)
            doc.update(result=result, expires=time.monotonic() + self.ttl)

    def release(self, job_id, owner):
        with self.lock:
            doc = self.records.get(job_id)
            if doc and doc["owner"] == owner:
                doc.update(owner=None, lease_until=0)

    def close(self):
        pass


# Redis TIME을 사용해 워커 간 시스템 시각 차이를 피한다. 결과 JSON은 Lua에서
# 해석하지 않는 문자열로 유지해 큰 Java Long ID·빈 배열이 변형되지 않게 한다.
MUTATE = """
local op, fingerprint, owner = ARGV[1], ARGV[2], ARGV[3]
local ttl, lease = tonumber(ARGV[4]), tonumber(ARGV[5])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local raw = redis.call('GET', KEYS[1])
local doc = raw and cjson.decode(raw) or nil
if op == 'acquire' then
    if not doc then doc = {fingerprint=fingerprint, owner='', lease_until=0, result=''} end
    if doc.fingerprint ~= fingerprint then return {'CONFLICT', ''} end
    if doc.result ~= '' then return {'CACHED', doc.result} end
    if doc.owner ~= '' and doc.lease_until > now then return {'BUSY', ''} end
    doc.owner = owner
    doc.lease_until = now + lease
elseif op == 'release' then
    if not doc or doc.owner ~= owner then return {'RELEASED', ''} end
    doc.owner = ''
    doc.lease_until = 0
else
    if not doc or doc.owner ~= owner or doc.lease_until <= now then return {'LOST', ''} end
    if op == 'renew' then doc.lease_until = now + lease end
    if op == 'finish' then doc.result = ARGV[6] end
end
redis.call('SET', KEYS[1], cjson.encode(doc), 'EX', ttl)
return {'ACQUIRED', ''}
"""


class RedisJobStore:
    """기존 Redis 연결 설정만 재사용하는 SQS 전용 원자적 실행·결과 기록."""

    def __init__(self, client, *, scope="default", ttl=86400, lease_seconds=960):
        self.client, self.ttl, self.lease_seconds = client, ttl, lease_seconds
        self.prefix = f"audigo:sqs:{scope}:job:"

    def _call(self, operation, job_id, owner, *, fingerprint="", result=""):
        state, value = self.client.eval(
            MUTATE, 1, self.prefix + str(job_id), operation, fingerprint, owner,
            self.ttl, self.lease_seconds, result,
        )
        if state == "CONFLICT":
            raise JobConflict("동일 작업 ID에 다른 요청이 전달됐습니다.")
        if state == "LOST":
            raise LeaseLost("SQS 실행 소유권이 만료됐습니다.")
        return Claim(state, value or None)

    def acquire(self, job_id, fingerprint, owner):
        return self._call("acquire", job_id, owner, fingerprint=fingerprint)

    def renew(self, job_id, owner):
        self._call("renew", job_id, owner)

    def finish(self, job_id, owner, result):
        self._call("finish", job_id, owner, result=result)

    def release(self, job_id, owner):
        self._call("release", job_id, owner)

    def close(self):
        self.client.close()


def request_fingerprint(request: dict) -> str:
    import hashlib
    raw = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
