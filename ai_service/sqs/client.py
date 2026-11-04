"""SQS SDK 호출을 이벤트 루프 밖에서 실행한다. AWS 자격증명은 기본 체인 사용."""
import asyncio

from ai_service.sqs.config import SqsSettings


class SqsTransportError(RuntimeError):
    pass


class SqsClient:
    def __init__(self, sdk, settings: SqsSettings):
        self.sdk, self.settings = sdk, settings

    @classmethod
    def from_settings(cls, settings: SqsSettings):
        settings.require_configured()
        # FastAPI import/startup은 boto3 또는 AWS metadata에 접근하지 않는다.
        import boto3
        from botocore.config import Config

        sdk = boto3.client(
            "sqs", region_name=settings.region, endpoint_url=settings.endpoint_url,
            config=Config(connect_timeout=5, read_timeout=25,
                          retries={"mode": "standard", "total_max_attempts": 3}),
        )
        return cls(sdk, settings)

    async def _call(self, operation, **values):
        try:
            return await asyncio.to_thread(getattr(self.sdk, operation), **values)
        except Exception as exc:
            # URL·message body·AWS 응답 본문을 오류 메시지에 포함하지 않는다.
            raise SqsTransportError(operation) from exc

    async def receive(self):
        response = await self._call(
            "receive_message", QueueUrl=self.settings.job_queue_url,
            MaxNumberOfMessages=1, WaitTimeSeconds=self.settings.wait_time_seconds,
            VisibilityTimeout=self.settings.visibility_timeout,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        return response.get("Messages", [])

    async def send_result(self, message):
        response = await self._call(
            "send_message", QueueUrl=self.settings.result_queue_url,
            MessageBody=message.model_dump_json(),
        )
        if not response.get("MessageId"):
            raise SqsTransportError("send_message_unconfirmed")

    async def delete(self, receipt):
        await self._call("delete_message", QueueUrl=self.settings.job_queue_url, ReceiptHandle=receipt)

    async def renew_visibility(self, receipt):
        await self._call(
            "change_message_visibility", QueueUrl=self.settings.job_queue_url,
            ReceiptHandle=receipt, VisibilityTimeout=self.settings.visibility_timeout,
        )

    async def close(self):
        await asyncio.to_thread(self.sdk.close)
