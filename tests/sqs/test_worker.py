import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

from ai_service.core.config import Settings
from ai_service.core.errors import GenerationFailed, MusicRecommendationFailed
from ai_service.services.travel.pipeline import generation_stages
from ai_service.sqs.client import SqsTransportError
from ai_service.sqs.idempotency import MemoryJobStore
from ai_service.sqs.messages import JobMessage, result_message
from ai_service.sqs.worker import SqsWorker
from tests.sqs.test_contract import job_payload
from tests.travel.test_compact_routes import PlaceClient, Planner


SETTINGS = Settings(api_token="test-only", openai_api_key="test-only", kakao_rest_api_key="test-only")


def delivery(payload=None, receipt="receipt"):
    return {"Body": json.dumps(payload or job_payload()), "ReceiptHandle": receipt}


class FakeSqs:
    def __init__(self):
        self.messages, self.deleted, self.events = [], [], []
        self.fail_kind = None
        self.renew_visibility = AsyncMock()

    async def send_result(self, message):
        self.messages.append(message.model_copy(deep=True))
        self.events.append(("send", message.type))
        if message.type == self.fail_kind:
            raise SqsTransportError("send_message")

    async def delete(self, receipt):
        self.events.append(("delete", receipt))
        self.deleted.append(receipt)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sqs = FakeSqs()
        self.store = MemoryJobStore()
        self.worker = SqsWorker(self.sqs, self.store, SETTINGS)

    async def run_real_pipeline(self, payload=None):
        with patch("ai_service.sqs.worker.KakaoPlaces", return_value=PlaceClient()), \
             patch("ai_service.sqs.worker.OpenAIPlanner", return_value=Planner()), \
             patch("ai_service.sqs.worker.generation_stages", wraps=generation_stages) as pipeline:
            ok = await self.worker.process_message(delivery(payload))
        return ok, pipeline

    def fake_completed(self):
        return result_message(JobMessage.model_validate(job_payload()), "COMPLETED",
                              stage="ROUTE_OPTIMIZE", state="DONE", payload={"title": "여행", "days": []})

    async def test_reuses_actual_four_stage_pipeline_and_preserves_job_id(self):
        with patch("httpx.AsyncClient.send", side_effect=AssertionError("external HTTP forbidden")):
            ok, pipeline = await self.run_real_pipeline()
        self.assertTrue(ok)
        self.assertEqual(pipeline.call_count, 1)
        internal = pipeline.call_args.args[0]
        self.assertEqual(internal.travel_plan_id, 10)
        self.assertTrue(all(msg.generation_job_id == 41 for msg in self.sqs.messages))
        self.assertEqual([(m.stage, m.state) for m in self.sqs.messages if m.type == "PROGRESS"], [
            (stage, state) for stage in ("PLACE_RECOMMEND", "STAY_RECOMMEND", "ROUTE_OPTIMIZE", "MUSIC_RECOMMEND")
            for state in ("RUNNING", "DONE")])
        final = self.sqs.messages[-1]
        self.assertEqual(final.type, "COMPLETED")
        self.assertEqual(set(final.payload), {"title", "days", "music"})
        self.assertTrue(final.payload["days"])
        self.assertEqual(self.sqs.events[-2:], [("send", "COMPLETED"), ("delete", "receipt")])

    async def test_generation_failure_sends_failed_before_deletion(self):
        planner = Planner()
        planner.generate = AsyncMock(side_effect=GenerationFailed("시간 조건을 충족하지 못했습니다."))
        with patch("ai_service.sqs.worker.KakaoPlaces", return_value=PlaceClient()), \
             patch("ai_service.sqs.worker.OpenAIPlanner", return_value=planner):
            ok = await self.worker.process_message(delivery())
        self.assertTrue(ok)
        failed = self.sqs.messages[-1]
        self.assertEqual((failed.type, failed.stage, failed.state), ("FAILED", "PLACE_RECOMMEND", "FAILED"))
        self.assertEqual(failed.error_message, "시간 조건을 충족하지 못했습니다.")
        self.assertEqual(self.sqs.events[-2:], [("send", "FAILED"), ("delete", "receipt")])

    async def test_existing_music_fallback_still_produces_completed_itinerary(self):
        planner = Planner()
        planner.recommend_music = AsyncMock(side_effect=MusicRecommendationFailed())
        with patch("ai_service.sqs.worker.KakaoPlaces", return_value=PlaceClient()), \
             patch("ai_service.sqs.worker.OpenAIPlanner", return_value=planner):
            self.assertTrue(await self.worker.process_message(delivery()))
        self.assertEqual(self.sqs.messages[-1].type, "COMPLETED")
        self.assertIsNotNone(self.sqs.messages[-1].payload["music"])

    async def test_invalid_request_reports_failed_without_external_calls(self):
        payload = job_payload(); payload["request"].pop("region_id")
        with patch("ai_service.sqs.worker.KakaoPlaces") as places:
            self.assertTrue(await self.worker.process_message(delivery(payload)))
        places.assert_not_called()
        self.assertEqual(self.sqs.messages[-1].type, "FAILED")
        self.assertEqual(self.sqs.messages[-1].generation_job_id, 41)

    async def test_malformed_or_unknown_version_message_is_not_deleted(self):
        for body in ("not-json", "{}", json.dumps({**job_payload(), "message_version": "2"})):
            self.assertFalse(await self.worker.process_message({"Body": body, "ReceiptHandle": "receipt"}))
        self.assertEqual(self.sqs.deleted, [])
        self.assertEqual(self.sqs.messages, [])

    async def test_result_send_failure_keeps_source_and_replays_cached_result(self):
        self.sqs.fail_kind = "COMPLETED"
        with patch.object(self.worker, "_generate", AsyncMock(return_value=self.fake_completed())) as generate:
            self.assertFalse(await self.worker.process_message(delivery()))
            self.assertEqual(self.sqs.deleted, [])
            first = self.sqs.messages[-1]
            self.sqs.fail_kind = None
            self.assertTrue(await self.worker.process_message(delivery(receipt="new-receipt")))
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(self.sqs.messages[-1].model_dump(), first.model_dump())
        self.assertEqual(self.sqs.deleted, ["new-receipt"])

    async def test_progress_delivery_failure_does_not_mark_generation_failed_or_delete(self):
        self.sqs.fail_kind = "PROGRESS"
        ok, _ = await self.run_real_pipeline()
        self.assertFalse(ok)
        self.assertEqual([m.type for m in self.sqs.messages], ["PROGRESS"])
        self.assertEqual(self.sqs.deleted, [])
        self.sqs.fail_kind = None
        ok, _ = await self.run_real_pipeline()
        self.assertTrue(ok)

    async def test_delete_failure_redelivery_does_not_regenerate(self):
        real_delete = self.sqs.delete
        with patch.object(self.worker, "_generate", AsyncMock(return_value=self.fake_completed())) as generate:
            self.sqs.delete = AsyncMock(side_effect=SqsTransportError("delete_message"))
            self.assertFalse(await self.worker.process_message(delivery()))
            self.sqs.delete = real_delete
            self.assertTrue(await self.worker.process_message(delivery()))
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(len({m.event_id for m in self.sqs.messages}), 1)

    async def test_duplicate_event_with_same_job_id_reuses_terminal_result(self):
        second = job_payload(); second["event_id"] = "another-outbox-delivery"
        with patch.object(self.worker, "_generate", AsyncMock(return_value=self.fake_completed())) as generate:
            self.assertTrue(await self.worker.process_message(delivery()))
            self.assertTrue(await self.worker.process_message(delivery(second)))
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(len(self.sqs.deleted), 2)

    async def test_conflicting_request_never_overwrites_existing_result(self):
        second = job_payload(); second["request"]["headcount"] = 9
        with patch.object(self.worker, "_generate", AsyncMock(return_value=self.fake_completed())) as generate:
            self.assertTrue(await self.worker.process_message(delivery()))
            self.assertFalse(await self.worker.process_message(delivery(second, "conflict")))
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(self.sqs.deleted, ["receipt"])

    async def test_running_duplicate_does_not_start_second_generation(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def generate(job, stage):
            entered.set()
            await finish.wait()
            return self.fake_completed()
        with patch.object(self.worker, "_generate", side_effect=generate) as mock:
            task = asyncio.create_task(self.worker.process_message(delivery()))
            await entered.wait()
            self.assertFalse(await self.worker.process_message(delivery(receipt="duplicate")))
            finish.set()
            self.assertTrue(await task)
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(self.sqs.deleted, ["receipt"])

    async def test_heartbeat_failure_cancels_work_and_keeps_source(self):
        cancelled = asyncio.Event()
        async def hang(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.worker.heartbeat_seconds = 0.001
        self.sqs.renew_visibility.side_effect = SqsTransportError("visibility")
        with patch.object(self.worker, "_generate", side_effect=hang):
            self.assertFalse(await self.worker.process_message(delivery()))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.sqs.deleted, [])
        self.assertEqual(self.sqs.messages, [])

    async def test_cancellation_releases_lease_without_terminal_message(self):
        entered = asyncio.Event()
        async def hang(*args):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.worker, "_generate", side_effect=hang):
            task = asyncio.create_task(self.worker.process_message(delivery()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.sqs.messages, [])
        self.assertEqual(self.sqs.deleted, [])
        ok, _ = await self.run_real_pipeline()
        self.assertTrue(ok)

    async def test_generation_timeout_publishes_safe_failed(self):
        self.worker.settings = replace(SETTINGS, generation_timeout_seconds=0.01)
        async def hang(*args):
            await asyncio.Event().wait()
        with patch.object(self.worker, "_generate", side_effect=hang):
            self.assertTrue(await self.worker.process_message(delivery()))
        self.assertEqual(self.sqs.messages[-1].type, "FAILED")
        self.assertIn("제한 시간", self.sqs.messages[-1].error_message)

    async def test_unexpected_error_does_not_expose_exception_text(self):
        with patch.object(self.worker, "_generate", AsyncMock(side_effect=RuntimeError("secret-key"))):
            self.assertTrue(await self.worker.process_message(delivery()))
        self.assertNotIn("secret-key", self.sqs.messages[-1].model_dump_json())

    async def test_run_stops_without_processing_new_message_after_shutdown(self):
        stop = asyncio.Event()
        async def receive():
            stop.set()
            return [delivery()]
        self.sqs.receive = AsyncMock(side_effect=receive)
        await self.worker.run(stop)
        self.assertEqual(self.sqs.messages, [])
        self.assertEqual(self.sqs.deleted, [])
