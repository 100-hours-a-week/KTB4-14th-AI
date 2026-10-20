from concurrent.futures import ThreadPoolExecutor
import json
import time
import unittest
from unittest.mock import patch

import fakeredis

from ai_service.sqs.idempotency import JobConflict, LeaseLost, MemoryJobStore, RedisJobStore, request_fingerprint


class StoreChecks:
    def test_concurrent_acquire_allows_only_one_owner(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda n: self.store.acquire(41, "hash", str(n)), range(16)))
        self.assertEqual(sum(x.state == "ACQUIRED" for x in results), 1)
        self.assertEqual(sum(x.state == "BUSY" for x in results), 15)

    def test_release_allows_retry_and_old_owner_cannot_overwrite(self):
        self.store.acquire(41, "hash", "first")
        self.store.release(41, "first")
        self.assertEqual(self.store.acquire(41, "hash", "second").state, "ACQUIRED")
        with self.assertRaises(LeaseLost):
            self.store.finish(41, "first", "wrong")
        self.store.release(41, "first")
        self.assertEqual(self.store.acquire(41, "hash", "third").state, "BUSY")

    def test_terminal_cache_survives_release_and_preserves_large_ids_and_empty_lists(self):
        job_id = 9223372036854775807
        result = json.dumps({"generation_job_id": job_id, "payload": {"days": [], "music": None}})
        self.store.acquire(job_id, "hash", "first")
        self.store.finish(job_id, "first", result)
        self.store.release(job_id, "first")
        cached = self.store.acquire(job_id, "hash", "second")
        self.assertEqual(cached.state, "CACHED")
        self.assertEqual(cached.result, result)

    def test_same_job_id_different_request_is_rejected(self):
        self.store.acquire(41, "hash", "owner")
        with self.assertRaises(JobConflict):
            self.store.acquire(41, "other", "other-owner")

    def test_heartbeat_keeps_ownership(self):
        self.store.acquire(41, "hash", "owner")
        self.store.renew(41, "owner")
        self.assertEqual(self.store.acquire(41, "hash", "duplicate").state, "BUSY")
        with self.assertRaises(LeaseLost):
            self.store.renew(41, "wrong")


class MemoryTests(StoreChecks, unittest.TestCase):
    def setUp(self):
        self.store = MemoryJobStore()

    def test_expired_lease_fences_old_worker_and_can_be_reclaimed(self):
        with patch("ai_service.sqs.idempotency.time.monotonic", return_value=1000):
            self.store.acquire(41, "hash", "dead")
        with patch("ai_service.sqs.idempotency.time.monotonic", return_value=2000):
            self.assertEqual(self.store.acquire(41, "hash", "new").state, "ACQUIRED")
            with self.assertRaises(LeaseLost):
                self.store.finish(41, "dead", "stale")

    def test_cache_is_bounded_and_does_not_evict_running_work(self):
        store = MemoryJobStore(capacity=1)
        store.acquire(1, "one", "owner")
        with self.assertRaises(RuntimeError):
            store.acquire(2, "two", "other")
        store.finish(1, "owner", "result")
        self.assertEqual(store.acquire(2, "two", "other").state, "ACQUIRED")
        self.assertEqual(len(store.records), 1)


class RedisTests(StoreChecks, unittest.TestCase):
    def setUp(self):
        self.client = fakeredis.FakeRedis(decode_responses=True)
        self.store = RedisJobStore(self.client)

    def test_redis_store_is_shared_between_worker_instances_and_isolated_from_celery(self):
        self.client.set("audigo:jobs:record:original", "unchanged")
        other = RedisJobStore(self.client)
        self.store.acquire(41, "hash", "first")
        self.assertEqual(other.acquire(41, "hash", "second").state, "BUSY")
        self.store.finish(41, "first", '{"payload":{"days":[]}}')
        self.assertEqual(other.acquire(41, "hash", "second").state, "CACHED")
        self.assertEqual(self.client.get("audigo:jobs:record:original"), "unchanged")

    def test_expired_lease_is_reclaimed_without_accepting_stale_finish(self):
        store = RedisJobStore(self.client, lease_seconds=0.01)
        store.acquire(41, "hash", "dead")
        time.sleep(0.03)
        with self.assertRaises(LeaseLost):
            store.finish(41, "dead", "stale")
        self.assertEqual(store.acquire(41, "hash", "new").state, "ACQUIRED")

    def test_queue_namespaces_do_not_share_job_numbers(self):
        self.store.acquire(41, "hash", "first")
        other = RedisJobStore(self.client, scope="different-queue")
        self.assertEqual(other.acquire(41, "different-request", "second").state, "ACQUIRED")


class FingerprintTests(unittest.TestCase):
    def test_key_order_is_ignored_but_request_content_is_not(self):
        self.assertEqual(request_fingerprint({"a": 1, "b": []}), request_fingerprint({"b": [], "a": 1}))
        self.assertNotEqual(request_fingerprint({"a": 1}), request_fingerprint({"a": 2}))
