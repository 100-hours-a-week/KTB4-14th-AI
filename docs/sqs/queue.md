# SQS 기반 일정 생성

비동기 일정 생성은 백엔드가 요청 SQS에 메시지를 발행하고 AI 워커가 수신하는 방식입니다.
별도의 Redis·Celery 작업 큐, Celery worker·beat, AI 작업 접수 및 상태 조회 HTTP API는 사용하지 않습니다.
기존 JSON·SSE 일정 생성과 동기 동행자 매칭 HTTP API는 유지합니다.

## 책임과 흐름

1. 백엔드가 작업 ID와 일정 요청 스냅샷을 요청 SQS에 발행합니다.
2. AI 워커가 수신하고 기존 장소·숙소·경로·음악 생성 파이프라인을 실행합니다.
3. AI가 진행(PROGRESS)·완료(COMPLETED)·실패(FAILED)를 결과 SQS에 발행합니다.
4. 백엔드가 결과를 수신해 사용자용 작업 상태와 여행 DB를 갱신합니다.

AI의 HTTP `POST /api/ai/v1/itinerary-jobs` 및 `GET /api/ai/v1/jobs/{job_id}`는 제거했습니다.
SQS 작업 식별자는 백엔드가 발행한 `generation_job_id`이며 AI가 새 작업 ID를 만들지 않습니다.
SQS 메시지의 요청·결과 모델은 `ai_service/sqs/messages.py`에서 확인합니다.

## 실행

```bash
uv sync --frozen --extra dev
./.venv/bin/python -m ai_service.sqs.worker
```

필수 환경변수는 다음과 같습니다. 기존 `.env` 파일도 읽으며 같은 이름의 환경변수가 우선합니다.

```dotenv
AWS_REGION=ap-northeast-2
AUDIGO_AI_JOB_QUEUE_URL=https://요청큐주소
AUDIGO_AI_RESULT_QUEUE_URL=https://결과큐주소
OPENAI_API_KEY=일정생성용키
KAKAO_REST_API_KEY=장소및경로조회용키
```

AWS 자격증명은 SDK 기본 체인을 사용합니다. 배포에서는 실행 역할의 큐 수신·삭제·가시성 변경 및
결과 큐 발행 권한을 설정합니다. 로컬 호환 SQS는 선택 환경변수 `AUDIGO_SQS_ENDPOINT`를 사용합니다.
요청 큐와 결과 큐는 서로 다른 Standard Queue여야 합니다. 이 코드가 AWS 큐·IAM·DLQ를 생성하지는 않습니다.

컨테이너 실행 구성은 `compose.sqs.yml`입니다. Redis와 Celery 서비스를 함께 띄우지 않습니다.
이미지 안의 HTTP API와 SQS 워커는 별도 프로세스로 실행합니다. main 배포 워크플로가
SQS 워커까지 자동 배포하는 것으로 간주하면 안 되며, 워커 실행 서비스는 배포 환경에서 별도로 설정해야 합니다.

## 재전달과 실패

- 긴 폴링으로 한 번에 한 메시지를 수신하며 기본 가시성 제한 시간은 960초입니다.
- 처리 중에는 기본 60초마다 메시지 가시성과 실행 권한을 갱신합니다.
- 최종 결과를 기록하고 결과 큐 발행이 확인된 다음 원본 요청 메시지를 삭제합니다.
- 결과 발행·메시지 삭제·실행 권한 갱신 실패 시 원본 메시지를 삭제하지 않습니다.
  삭제되지 않은 메시지는 가시성 제한 시간이 지나면 다시 수신될 수 있습니다.
- 형식이 잘못되어 작업 ID를 신뢰할 수 없는 메시지도 임의 삭제하거나 FAILED 작업을 생성하지 않습니다.
- 생성 예외는 FAILED 결과로 확정하고, 해당 결과 발행이 확인되면 요청 메시지를 삭제합니다.
  생성 실패 자체가 자동 재추론되는 정책은 현재 구현되어 있지 않습니다.
- 반복 수신된 메시지의 DLQ 이동 기준과 `maxReceiveCount`는 AWS 요청 큐의 redrive policy에서 설정합니다.

SQS는 전달을 관리할 뿐 사용자용 작업 상태 DB가 아닙니다.
Standard Queue의 중복 수신 가능성은 [AWS 전달 보장](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/standard-queues-at-least-once-delivery.html)과
[가시성 제한 시간](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)을 참고하세요.

## 중복 실행과 결과 재발행

SQS 워커는 `generation_job_id`와 요청 해시로 실행 권한 및 완료 결과를 기록합니다.
같은 작업의 동시 실행은 막고, 이미 완료한 작업이 재수신되면 저장한 최종 결과를 다시 발행합니다.
결과 이벤트 ID는 같은 작업·단계·상태에서 일정하게 유지하여 백엔드가 결과 중복을 판별할 수 있게 합니다.
백엔드의 실제 중복 결과 반영 방지는 백엔드 결과 수신 구현에서도 처리해야 합니다.

기본 메모리 기록은 프로세스 하나에만 적용됩니다. 재시작하면 사라지며 여러 워커 사이에 공유되지 않습니다.
따라서 메모리만 사용하는 배포에서 중복 추론이나 정확히 한 번 실행을 보장하지 않습니다.

기존 SQS 어댑터의 선택 Redis 기록은 유지합니다. 이는 작업 큐·사용자용 상태 조회가 아닌
여러 워커 사이의 실행 권한과 최종 결과 재발행 기록용 보조 저장소입니다.
사용하려면 `JOBS_REDIS_URL`을 설정하며 보관 기간은 `JOB_TTL_SECONDS`(기본 86,400초)입니다.
기존 Redis 키·DB·계정·볼륨은 이 정리에서 삭제하지 않습니다.

## 검증

```bash
./.venv/bin/python -m unittest discover -s tests/sqs -t . -q
```

SDK Stubber와 테스트용 저장소로 메시지 계약·결과 발행 후 삭제·가시성 갱신·전송 장애·중복 실행을 검증합니다.
실제 AWS 큐를 호출하거나 생성하지 않습니다. 실제 배포의 IAM·큐 주소·네트워크·redrive policy는 별도 환경에서 검증해야 합니다.
