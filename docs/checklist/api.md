# 여행 체크리스트 생성

`POST /api/ai/v1/checklists`는 백엔드가 서비스 Bearer 토큰으로 호출하는 동기 API다.
AI는 생성 결과를 반환하며, 체크리스트와 항목의 DB 저장은 백엔드가 담당한다.

2026-11-01 확인한 [원본 명세의 AI 서비스 API 탭](https://docs.google.com/spreadsheets/d/1TzLjPrQyFacu3QHNvTs71D12u_Yt2-N8eZ9KSzThijs/edit?gid=562071274#gid=562071274)
26~31행을 기준으로 요청·응답·오류 예시를 맞췄다. 최초 채팅의 `trip_context` 형식은 현재 계약에서 사용하지 않는다.
원본에서 읽은 예시는 `tests/checklist/fixtures/sheet-contract.json`에 별도로 보관해 Swagger와 실제 응답을 대조한다.

## 실행과 호출

프로젝트 `.env`에 `AUDIGO_API_TOKEN`(최소 32자)과 `OPENAI_API_KEY`를 설정한다.
`OPEN_API_KEY`도 기존 호환 설정으로 지원한다. 모델은 `OPENAI_MODEL` 설정을 사용한다.
체크리스트에는 카카오 키, YouTube, E5 모델, SQS 또는 DB 연결이 필요하지 않다.

```bash
uv run --frozen uvicorn ai_service.main:app --host 0.0.0.0 --port 8000 --reload
```

`http://localhost:8000/docs`에서 토큰을 입력한 뒤 V3의 여행 체크리스트 생성을 실행한다.
아래 curl 예시는 셸에도 `AUDIGO_API_TOKEN`을 설정한 경우 사용할 수 있다.

```bash
curl --fail-with-body http://localhost:8000/api/ai/v1/checklists \
  -H "Authorization: Bearer ${AUDIGO_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{
    "travel_plan_id": 101,
    "region_name": "부산광역시",
    "arrival_datetime": "2026-11-10T13:00:00",
    "departure_datetime": "2026-11-11T18:00:00",
    "headcount": 2,
    "companion_type": "COUPLE",
    "preference": {"themes": ["NATURE", "FOOD"]},
    "itinerary_items": [{
      "day_number": 1,
      "date": "2026-11-10",
      "place_name": "광안리해수욕장",
      "item_type": "TOUR",
      "start_time": "15:00",
      "end_time": "16:00"
    }]
  }'
```

## 입력 계약

모든 예시 필드는 필수이며, 알 수 없는 필드는 400으로 거부한다.

| 필드 | 검증 |
| --- | --- |
| `travel_plan_id` | 양의 정수. 문자열·불리언·실수는 거부 |
| `region_name` | 앞뒤 공백 제거 후 1~500자 |
| `arrival_datetime`, `departure_datetime` | 날짜·시간이 포함된 ISO 8601 일시. 출발일시 ≥ 도착일시, 당일 여행 허용 |
| `headcount` | 양의 정수. 문자열·불리언·실수는 거부 |
| `companion_type` | 앞뒤 공백 제거 후 1~500자. 예: `COUPLE` |
| `preference.themes` | `NATURE`, `FOOD`, `CULTURE`, `REST`, `SNS`, `ACTIVITY` 중 중복 없이 1~3개 |
| `itinerary_items` | 필수 배열, 최대 500개. 상세 일정이 없으면 빈 배열 허용 |
| `itinerary_items[].day_number` | 1부터 시작하는 양의 정수. 여행 시작일과 `date`로 계산한 일차와 일치 |
| `itinerary_items[].date` | 유효한 `YYYY-MM-DD`, 여행 기간 내 날짜 |
| `itinerary_items[].place_name`, `item_type` | 앞뒤 공백 제거 후 1~500자. 유형 예: `TOUR` |
| `itinerary_items[].start_time`, `end_time` | 24시간제 `HH:MM` |

시간대 없는 도착·출발 일시는 한국 시간으로 해석한다. 둘 중 하나만 시간대가 있으면 거부하며,
둘 다 시간대가 있으면 한국 시간으로 변환한 날짜를 일정의 날짜·일차 검증에 사용한다.
`companion_type`과 `item_type`의 enum은 원본에서 확정하지 않아 특정 값 하나로 제한하지 않는다.
문구 길이·테마 개수·일정 개수 및 날짜 일관성은 서버의 추가 입력 검증 기준이며 위 표에 명시했다.
체크리스트 생성 모델에는 지역·일시·인원·동행·선호·상세 일정을 모두 전달하며,
일정이 비어 있으면 실제 장소나 활동을 지어내지 않도록 안내한다.

## 성공 응답

```json
{
  "travel_plan_id": 101,
  "title": "부산 바다 여행 준비물",
  "items": [
    {"content": "신분증 챙기기", "is_completed": false, "is_checked": false, "sort_order": 1},
    {"content": "휴대전화 충전기 챙기기", "is_completed": false, "is_checked": false, "sort_order": 2}
  ]
}
```

제목은 1~100자, 항목은 1~50개, 각 항목 내용은 1~200자다.
모델은 제목과 항목 내용만 생성한다. AI 서버가 요청의 `travel_plan_id`, 두 상태의 초기값 `false`,
1부터 연속한 `sort_order`를 부여한다. 공백뿐인 문구나 동일한 항목은 허용하지 않는다.
모델 출력이 유효하지 않으면 한 번 재생성하며, 두 번째도 실패하면 422를 반환한다.
모델 호출 장애는 자동 재생성하지 않고 503으로 반환한다.

두 상태 필드의 생성 이후 의미와 갱신은 백엔드·프론트 계약에서 확정한다.
같은 여행으로 다시 호출하면 새로 생성한다. 기존 항목 교체·추가 및 체크 상태 보존 정책은 백엔드가 결정한다.
AI 서버는 여행 ID의 DB 존재 여부나 소유권을 조회하지 않으므로 백엔드에서 확인한 여행 정보를 전달해야 한다.

## 오류 응답

| HTTP | `error.code` | 의미 | `retryable` |
| --- | --- | --- | --- |
| 400 | `INVALID_REQUEST` | 필수 값 누락, JSON·자료형·날짜·허용 범위 오류 | false |
| 401 | `UNAUTHORIZED` | 서비스 토큰 누락 또는 불일치 | false |
| 422 | `CHECKLIST_GENERATION_FAILED` | 모델 거절 또는 재생성 후에도 결과 검증 실패 | false |
| 500 | `INTERNAL_SERVER_ERROR` | 예상하지 못한 내부 오류 | false |
| 503 | `AI_SERVICE_UNAVAILABLE` | 모델 HTTP 오류·호출 제한·시간 초과·필수 설정 누락 | true |

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "입력값을 확인해 주세요.",
    "retryable": false,
    "details": [{"field": "body.region_name", "type": "missing"}]
  },
  "request_id": "req_checklist_example"
}
```

`details`는 실제 입력 오류에 따라 달라지며, 생성·서비스 오류에서는 빈 배열이다.
모든 응답의 `X-Request-Id` 헤더로 요청을 추적할 수 있다. 오류 본문의 `request_id`는 헤더와 같다.
외부 서비스 응답 본문·인증정보·모델 원문을 오류 응답에 노출하지 않는다.
개별 모델 호출과 전체 요청은 기존 `model_timeout_seconds`와 `generation_timeout_seconds` 설정을 사용한다.

모델 출력은 [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)의
JSON Schema로 제한하고, 길이·공백·중복은 서버에서 다시 검증한다.

## 자동 검증

```bash
uv run --frozen --extra dev pytest tests/checklist -q
```

테스트는 외부 모델 HTTP 응답을 모의 처리한다. 성공·입력 오류·인증·설정 누락·출력 재생성·거절·
모델 장애·전체 제한 시간 취소·내부 오류·Swagger 예시와 원본 시트 스냅샷을 검사하며 유료 모델 호출은 수행하지 않는다.

2026-11-01에는 별도로 로컬 FastAPI 라우터에서 실제 설정된 모델을 호출했다. 원본 시트의 부산 예시로
HTTP 200, 약 8.5초, 준비 항목 13개를 확인했다. 반환된 여행 ID, 두 초기 상태와 연속 정렬 순서도 검증했다.
11월 예보 확인과 광안리해수욕장 방문 준비가 결과에 반영됐다. 이는 한 요청의 통합 확인이며
모든 입력의 생성 품질·지연 시간이나 배포 서버의 상태를 보증하는 측정은 아니다.
