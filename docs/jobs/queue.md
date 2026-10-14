# AI 작업 큐 연동 계약

Redis + Celery를 사용합니다. FastAPI는 입력 검증과 작업 접수, Celery 워커는 AI 실행,
Redis는 공유 상태·결과·중복 방지·전달 대기 목록을 담당합니다. Spring은 사용자 인증,
최종 여행/매칭 결과의 DB 저장과 프론트 전달을 담당합니다. 매칭 후보는 기존 요청 전달 방식 또는
추가된 AI 워커의 MySQL 조회 방식으로 가져올 수 있습니다.

## API

모든 API는 기존 `Authorization: Bearer <AUDIGO_API_TOKEN>` 서비스 인증을 사용합니다.
로그인 사용자 토큰과 서비스 토큰을 혼용하지 않습니다.

| 기능 | 경로 | 정상 응답 |
| --- | --- | --- |
| 일정 작업 접수 | `POST /api/ai/v1/itinerary-jobs` | 202 |
| 동행자 추천 작업 접수 | `POST /api/ai/v1/traveler-match-jobs` | 202 |
| 백엔드 기존 동기 추천 | `POST /api/ai/v1/matches/candidates` + `{request,candidates}` | 200 |
| 작업 상태·결과 조회 | `GET /api/ai/v1/jobs/{job_id}` | 200 |

일정 요청 Body는 기존 두 DTO(중첩 `generation_job_id` 또는 평면 `travel_plan_id`)와 같습니다.
`title`, `days` 등 응답 필드를 요청에 넣지 않습니다. 기존 JSON/SSE API는 호환을 위해 유지합니다. Spring이 새 경로로 전환한 뒤 SSE 제거를 별도로 진행할 수 있습니다.

202는 Redis에 작업과 전달 대기 기록이 **동시에 저장된 접수 성공**입니다. 생성 성공을 의미하지
않습니다. 브로커 전송이 실패해도 Celery beat가 전달을 복구합니다. Redis 저장 실패는 503입니다.
워커/beat가 꺼져 있으면 작업은 QUEUED로 남으므로 두 서비스를 반드시 함께 운영해야 합니다.

```json
{
  "job_id": "job_itinerary_ab12cd34",
  "job_type": "ITINERARY_GENERATION",
  "status": "QUEUED",
  "status_url": "/api/ai/v1/jobs/job_itinerary_ab12cd34"
}
```

같은 요청의 재접수에는 기존 job_id와 현재 상태를 반환합니다. 조회 응답에는 job_id/job_type/status/
status_url, 원래 request_id, 현재 단계(current_stage/stage_status), 실행 횟수(attempt/max_attempts),
생성·갱신·만료 시각(created_at/updated_at/expires_at), 재시도 시각(next_retry_at), 요청 식별 ID,
최종 result와 error가 있습니다. 시각은 UTC ISO 8601입니다. 내부 입력·실행 소유권은 노출하지 않습니다.

상태: `QUEUED → RUNNING → COMPLETED` 또는 `FAILED`.
일시적 장애는 `RUNNING → RETRYING → RUNNING`입니다. Spring/프론트는 1~3초 간격으로 조회하고
COMPLETED/FAILED에서 조회를 종료합니다. 조회 자체가 200이어도 status=FAILED이면 생성 실패입니다.

## 결과 계약

일정 완료 result는 기존 SSE의 최종 result와 같은 `title`, `days`, `music`입니다.
4단계는 PLACE_RECOMMEND → STAY_RECOMMEND → ROUTE_OPTIMIZE → MUSIC_RECOMMEND 순서이며
음악 처리까지 마친 뒤에만 result를 기록하고 COMPLETED로 전환합니다.

`days[].items`는 place_type(TOURISM/RESTAURANT/ACCOMMODATION)을 사용하고 같은 날짜 경로는
`days[].routes`에 들어갑니다. 날짜를 넘는 이동은 다음날 첫 항목의 route_from_previous에 유지됩니다.
총요금·간결한 경로 필드는 기존 SSE와 같습니다. result는 Redis의 임시 결과이며 여행 DB 저장은 Spring의 책임입니다.

매칭 요청 예시(후보는 실제 Spring 조회 결과로 채우며 AI는 목데이터를 추가하지 않습니다):

추가된 MySQL 조회 방식은 식별자만 전달하는 요청을 지원합니다. 조건·후보를 DB에서 읽는 설정과
계약은 [MySQL 매칭 연동](../matching/mysql.md)을 참고하세요. 아래 후보 전달 방식도 계속 지원합니다.

현재 백엔드 매칭 테이블용 DB 요청은 다음과 같습니다. 기본 limit은 3이며 여행 ID는 필요하지 않습니다.

```json
{"matching_request_id":201,"requester_id":201,"limit":3}
```

후보 예산은 백엔드 프로필에 없으므로 비교하지 않습니다. 요청에 예산이 있으면 완료 결과에
`unapplied_conditions: ["budget"]`를 표시합니다. 기존 `/api/ai/v1/matches/candidates`에 ID 요청을 보내던 호출도 같은 큐의
202 접수로 호환합니다. 중첩 `{request,candidates}`는 [백엔드 동기 추천](../matching/backend-contract.md) 계약입니다. 아래 3개 ID와 후보를 전달하는 예시는 이전 계약의 호환 경로입니다.

```json
{
  "matching_request_id": 201,
  "travel_plan_id": 101,
  "requester_id": 12,
  "preferred_companion_gender": "ANY",
  "pace": "BALANCED",
  "budget_min": 300000,
  "budget_max": 800000,
  "themes": ["자연", "미식"],
  "candidates": [
    {
      "user_id": 35,
      "pace": "BALANCED",
      "themes": ["자연", "사진"],
      "budget_min": 300000,
      "budget_max": 800000
    }
  ],
  "limit": 10
}
```

필수: matching_request_id/travel_plan_id/requester_id/preferred_companion_gender/pace/themes/candidates.
선택: budget_min/budget_max/limit(기본10, 1~20).
각 후보의 필수 필드는 user_id/pace/themes입니다. gender(Female/Male/Other), 예산 구간,
introduction은 선택입니다. Any 조건에서는 성별을 생략할 수 있고 성별 조건이 있으면 미확인 후보는
제외합니다. 예산 조건이 있으면 후보의 budget_min/budget_max가 둘 다 필요합니다.
자기 자신을 제외하며 중복 후보 ID는 400입니다. 후보는 최대 200명입니다.

매칭 완료 result:

```json
{
  "matching_request_id": 201,
  "travel_plan_id": 101,
  "requester_id": 12,
  "recommendations": [{"user_id": 35, "score": 0.85, "rank": 1}]
}
```

점수는 기본 설정에서 E5 코사인 유사도이며, 선택 가중치를 설정하면 E5·테마·속도 조합 점수입니다.
어느 방식도 매칭 성공 확률이 아닙니다. 구성과 평가 기준은 [MySQL 매칭 연동](../matching/mysql.md)을 참고하세요.
모델은 기존 로컬 multilingual-e5-small을
재사용하며 모델 추론은 OpenAI를 호출하지 않습니다. ID 요청의 후보는 모델 추론 전에 MySQL에서 조회합니다.

## 오류·재시도·중복

접수 오류: 400 INVALID_REQUEST, 401 UNAUTHORIZED, 409 IDEMPOTENCY_CONFLICT,
422 NO_MATCH_CANDIDATES 또는 일정 조건 실패, 503 큐/필수 서비스 설정 오류.
없는 작업·만료 작업의 조회는 404 JOB_NOT_FOUND입니다.

```json
{
  "error": {
    "code": "NO_MATCH_CANDIDATES",
    "message": "추천할 동행 후보가 없습니다.",
    "retryable": false,
    "details": []
  },
  "request_id": "req_..."
}
```

접수 후 발생한 오류는 조회 응답의 error에 보관합니다. 기본 2회 재시도(총3회)이며 지연은
5초·10초의 지수 백오프, 최대60초입니다. 외부 서비스 일시 장애·작업 타임아웃은 재시도하고,
요청 조건 실패·모델 로딩/설정 누락·예상하지 못한 내부 오류는 자동 재시도하지 않습니다.
재시도는 전체 파이프라인을 다시 실행합니다. 단계별 체크포인트 재개는 포함하지 않습니다.

`Idempotency-Key` 헤더(1~128자)를 권장합니다. 생략하면 일정의 generation_job_id 또는 travel_plan_id,
매칭의 matching_request_id를 기본 키로 사용합니다. 같은 작업 유형·키·입력은 같은 작업을 반환하고,
같은 키에 다른 입력을 넣으면 409입니다. 다시 생성하려면 새 키를 사용합니다.

워커는 Redis의 원자적 실행 소유권을 획득한 경우에만 실행합니다. 중복 전달은 진행 중인 작업이나
완료 결과를 덮어쓰지 않습니다. 워커 중단 시 실행 소유권 만료 후 beat가 다시 전달하며 전체 실행
횟수를 제한합니다. 큐 전달은 최소 한 번 방식으로, 장애 시 외부 모델 호출 자체는 반복될 수 있습니다.
Spring의 최종 DB 저장에도 job_id 기준 중복 방지를 적용해야 합니다.

작업·결과·중복 키의 보관 기간은 접수부터 기본24시간(JOB_TTL_SECONDS)입니다.
만료 결과는 재조회할 수 없으므로 완료 후 Spring DB에 저장해야 합니다. Redis는 AOF 영속성과
noeviction 정책을 사용합니다. Redis Cluster는 현재 단일 인스턴스용 Lua 키 구성과 호환되지 않습니다.

## 실행

프로젝트의 .env에 기존 서비스 토큰과 OpenAI/Kakao 키를 설정합니다. 모델은 기존 배포 이미지에
포함되며 매칭 워커는 로컬 모델만 로딩합니다.

Docker Desktop/엔진을 실행한 뒤:

```bash
docker compose -f compose.jobs.yml up --build -d
docker compose -f compose.jobs.yml logs -f api worker beat
```

Compose는 Redis 볼륨, API, Celery worker, beat를 실행합니다. Redis 상태는 DB1, 브로커는 DB0입니다.
외부 연결 주소로 배포할 경우 서비스 토큰·접근 제한·Redis 연결 설정은 클라우드 팀 설정을 적용합니다.

이미 로컬 Redis가 있다면 .env.example의 큐 설정을 .env에 추가하고 터미널 세 개에서 실행합니다:

```bash
uv sync --frozen
./.venv/bin/python -m uvicorn ai_service.main:app --host 127.0.0.1 --port 8000
```

```bash
./.venv/bin/celery -A ai_service.jobs.worker:celery_app worker --loglevel=INFO --concurrency=2 --queues=audigo_itineraries,audigo_matching,audigo_control
```

```bash
./.venv/bin/celery -A ai_service.jobs.worker:celery_app beat --loglevel=INFO --schedule=/tmp/audigo-beat
```

/docs에서 작업 API를 POST한 뒤 status_url을 GET하여 최종 status/result를 확인합니다.
AI 내부 서비스 API이므로 프론트는 Spring을 통해 접근합니다. 브라우저 새로고침으로 작업이 취소되지 않습니다.

## 검증 재현

```bash
uv sync --frozen --extra dev
./.venv/bin/python -m unittest discover -s tests/jobs -t . -p 'test_jobs.py' -v
```

실제 Redis/Celery 워커 통합 테스트는 별도의 임시 Redis를 실행하고 종료합니다. 외부 유료 API는
호출하지 않으며 일정 제공사는 모의 응답, 동행자 매칭은 고정 로컬 E5 모델을 사용합니다.
redis-server 실행 파일이 없으면 이 통합 테스트는 skip되며 일반 단위 테스트는 실행됩니다.

```bash
REDIS_SERVER=/path/to/redis-server ./.venv/bin/python -m unittest discover -s tests/jobs -t . -p 'test_jobs_integration.py' -v
```
