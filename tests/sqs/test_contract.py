from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_service.core.config import Settings
from ai_service.main import create_app
from ai_service.sqs.client import SqsClient, SqsTransportError
from ai_service.sqs.config import SqsSettings
from ai_service.sqs.messages import JobMessage, ResultMessage, result_message
from tests.travel.test_day_transport import http_request, request


SQS = SqsSettings(job_queue_url="https://sqs.test/jobs", result_queue_url="https://sqs.test/results")


def job_payload():
    return dict(event_id="source-event", generation_job_id=41, message_version="1",
                occurred_at="2026-10-20T01:00:00Z", request=http_request(request(None, "CAR")))


class MessageTests(unittest.TestCase):
    def test_job_parsing_preserves_backend_job_id_and_full_snapshot(self):
        job = JobMessage.model_validate_json(json.dumps(job_payload()))
        self.assertEqual(job.generation_job_id, 41)
        self.assertEqual(job.request["travel_plan_id"], 10)
        self.assertEqual(job.occurred_at.utcoffset().total_seconds(), 0)

    def test_required_envelope_fields_and_types(self):
        for field in job_payload():
            with self.subTest(missing=field):
                data = job_payload(); data.pop(field)
                with self.assertRaises(ValidationError):
                    JobMessage.model_validate(data)
        invalid = [("event_id", ""), ("event_id", "  "), ("generation_job_id", True),
                   ("generation_job_id", "41"), ("generation_job_id", 0),
                   ("message_version", "2"), ("occurred_at", "2026-10-20T01:00:00"),
                   ("request", []), ("request", None)]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                JobMessage.model_validate({**job_payload(), field: value})

    def test_backend_extension_fields_are_ignored(self):
        self.assertEqual(JobMessage.model_validate({**job_payload(), "future": True}).generation_job_id, 41)

    def test_result_content_rules(self):
        common = {key: value for key, value in job_payload().items() if key != "request"}
        for values in ({"type": "PROGRESS"}, {"type": "PROGRESS", "stage": "PLACE_RECOMMEND"},
                       {"type": "COMPLETED"}, {"type": "FAILED"},
                       {"type": "FAILED", "error_message": " "},
                       {"type": "PROGRESS", "stage": "PLACES", "state": "DONE"},
                       {"type": "PROGRESS", "stage": "PLACE_RECOMMEND", "state": "COMPLETED"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ResultMessage.model_validate({**common, **values})
        for values in ({"type": "PROGRESS", "stage": "PLACE_RECOMMEND", "state": "RUNNING"},
                       {"type": "COMPLETED", "payload": {"days": []}},
                       {"type": "FAILED", "error_message": "실패"}):
            self.assertEqual(ResultMessage.model_validate({**common, **values}).generation_job_id, 41)

    def test_repeated_results_have_stable_ids_and_distinct_stage_ids(self):
        a = JobMessage.model_validate(job_payload())
        b = a.model_copy(update={"event_id": "redelivered-event"})
        x = result_message(a, "COMPLETED", payload={"days": []})
        y = result_message(b, "COMPLETED", payload={"days": []})
        self.assertEqual(x.event_id, y.event_id)
        self.assertNotEqual(x.event_id, result_message(a, "FAILED", error_message="실패").event_id)
        self.assertNotEqual(result_message(a, "PROGRESS", stage="PLACE_RECOMMEND", state="RUNNING").event_id,
                            result_message(a, "PROGRESS", stage="PLACE_RECOMMEND", state="DONE").event_id)


class ConfigurationTests(unittest.TestCase):
    def test_only_dedicated_worker_requires_queue_settings(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            config = SqsSettings.from_env(Path(folder) / "missing.env")
        self.assertEqual(config.region, "ap-northeast-2")
        self.assertIsNone(config.endpoint_url)
        with self.assertRaises(ValueError):
            config.require_configured()
        with patch("boto3.client", side_effect=AssertionError("AWS must not be accessed")):
            with TestClient(create_app(settings=Settings(api_token="test-only"))) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/docs").status_code, 200)

    def test_environment_overrides_dotenv_and_optional_endpoint(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "env"
            path.write_text("AWS_REGION=old\nAUDIGO_AI_JOB_QUEUE_URL=https://sqs.test/jobs\n"
                            "AUDIGO_AI_RESULT_QUEUE_URL=https://sqs.test/results\n")
            with patch.dict(os.environ, {"AWS_REGION": "ap-northeast-2", "AUDIGO_SQS_ENDPOINT": "http://localhost:4566"}, clear=True):
                config = SqsSettings.from_env(path)
        config.require_configured()
        self.assertEqual(config.endpoint_url, "http://localhost:4566")
        self.assertEqual(config.region, "ap-northeast-2")

    def test_client_uses_aws_default_credentials_chain(self):
        with patch("boto3.client") as factory:
            SqsClient.from_settings(SQS)
        kwargs = factory.call_args.kwargs
        self.assertNotIn("aws_access_key_id", kwargs)
        self.assertNotIn("aws_secret_access_key", kwargs)
        self.assertEqual(kwargs["region_name"], "ap-northeast-2")
        self.assertGreater(kwargs["config"].read_timeout, SQS.wait_time_seconds)

    def test_invalid_or_fifo_configuration_is_rejected(self):
        for values in ({"job_queue_url": "same", "result_queue_url": "same"},
                       {"job_queue_url": "https://sqs.test/jobs.fifo", "result_queue_url": "results"},
                       {"job_queue_url": "jobs", "result_queue_url": "results", "wait_time_seconds": 21}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SqsSettings(**values).require_configured()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # SDK Stubber uses explicit non-production test credentials, never AWS metadata.
        self.sdk = boto3.client("sqs", region_name="ap-northeast-2",
                                aws_access_key_id="testing", aws_secret_access_key="testing")
        self.stub = Stubber(self.sdk)
        self.client = SqsClient(self.sdk, SQS)

    async def asyncTearDown(self):
        self.stub.assert_no_pending_responses()
        await self.client.close()

    async def test_receive_uses_long_poll_one_message_and_visibility(self):
        values = {"QueueUrl": SQS.job_queue_url, "MaxNumberOfMessages": 1,
                  "WaitTimeSeconds": 20, "VisibilityTimeout": 960,
                  "MessageSystemAttributeNames": ["ApproximateReceiveCount"]}
        self.stub.add_response("receive_message", {}, values)
        with self.stub:
            self.assertEqual(await self.client.receive(), [])

    async def test_send_delete_and_visibility_use_correct_queue_and_receipt(self):
        msg = result_message(JobMessage.model_validate(job_payload()), "COMPLETED", payload={"days": []})
        self.stub.add_response("send_message", {"MessageId": "result-id"},
                               {"QueueUrl": SQS.result_queue_url, "MessageBody": msg.model_dump_json()})
        self.stub.add_response("change_message_visibility", {},
                               {"QueueUrl": SQS.job_queue_url, "ReceiptHandle": "receipt", "VisibilityTimeout": 960})
        self.stub.add_response("delete_message", {},
                               {"QueueUrl": SQS.job_queue_url, "ReceiptHandle": "receipt"})
        with self.stub:
            await self.client.send_result(msg)
            await self.client.renew_visibility("receipt")
            await self.client.delete("receipt")

    async def test_send_failure_is_transport_error_without_payload_or_url(self):
        msg = result_message(JobMessage.model_validate(job_payload()), "FAILED", error_message="실패")
        self.stub.add_client_error("send_message", service_error_code="AccessDenied",
                                   service_message="secret body", http_status_code=403)
        with self.stub, self.assertRaises(SqsTransportError) as error:
            await self.client.send_result(msg)
        self.assertEqual(str(error.exception), "send_message")

    async def test_unconfirmed_send_does_not_count_as_delivery(self):
        msg = result_message(JobMessage.model_validate(job_payload()), "FAILED", error_message="실패")
        self.stub.add_response("send_message", {})
        with self.stub, self.assertRaises(SqsTransportError):
            await self.client.send_result(msg)
