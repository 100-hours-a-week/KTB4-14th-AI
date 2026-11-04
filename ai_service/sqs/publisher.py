"""기존 생성 단계 이름을 백엔드의 Result 메시지로 변환·발행한다."""
from ai_service.services.travel.backend_contract import STAGE_NAMES
from ai_service.sqs.messages import result_message


class ResultPublisher:
    def __init__(self, client):
        self.client = client

    async def progress(self, job, stage, status):
        state = {"STARTED": "RUNNING", "COMPLETED": "DONE"}[status]
        message = result_message(job, "PROGRESS", stage=STAGE_NAMES[stage], state=state)
        await self.publish(message)

    async def publish(self, message):
        await self.client.send_result(message)
