# AUDIGO AI API

여행 조건과 필수 방문 장소를 받아 날짜별 여행 일정과 방문 순서를 생성하는 FastAPI 기반 V1 AI 서비스입니다. 카카오에서 장소 후보를 조회하고 OpenAI로 일정과 여행 제목을 생성합니다. 대중교통·도보 경로는 카카오맵 REST API를 사용하며, 음악은 백엔드에서 전달한 후보 중 한 곡을 선택합니다.

이 README는 AI 레포지토리의 설정·실행·API 연동 안내입니다. 응답 필드와 경로 좌표의 의미는 [여행 일정 API 용어 설명](API_TERMS.md)에 정리했습니다. 날짜별 개발 기록과 발표 자료는 별도로 관리합니다.

## 서비스 범위

- 지역, 여행 기간, 인원, 동행 유형, 여행 속도, 이동수단, 예산, 거리 선호도, 테마, 음식 취향, 추가 요청을 반영합니다.
- 필수 장소와 지정 순서를 검증하고 관광·식당·숙소로 일정을 구성합니다.
- AI가 날짜별 필수 카테고리를 빠뜨리면 실제 카카오 후보로 보완하고 숙소를 하루 마지막에 배치합니다. 이후 이동·체류시간과 필수 장소를 다시 검증하며, 조건을 만족하지 못한 결과는 성공으로 반환하지 않습니다.
- `RELAXED`, `BALANCED`, `PACKED`에 따라 하루 방문 수와 체류시간을 조정합니다.
- 기존 최종 JSON API와 단계별 결과를 전달하는 SSE API를 제공합니다.
- AI 서버는 `travel_plan`을 DB에 저장하지 않습니다. 사용자가 최종 확정한 일정의 저장은 백엔드의 별도 API가 담당합니다.

현재 구현 범위는 V1입니다. 작업 큐, 작업 상태 DB, 중단된 요청 복구, V2/V3 기능은 포함하지 않습니다.

## 실행 환경

- Python 3.11 이상, 3.15 미만
- 패키지 설치: `pip` 및 `requirements.txt`
- FastAPI, Uvicorn, Pydantic, HTTPX
- 외부 API: OpenAI, Kakao Local, 카카오맵 경로 조회

현재 저장소에서는 서버 파일이 `development/`에 있습니다. AI 전용 레포지토리로 옮길 때는 **이 폴더의 내용을 레포지토리 루트에 배치**할 수 있습니다. 아래 명령은 `requirements.txt`가 있는 디렉터리에서 실행합니다.

## 설치 및 실행

현재 저장소에서 작업한다면 먼저 `cd development`로 이동합니다. AI 전용 레포지토리 루트에 서버 파일을 배치했다면 디렉터리를 추가로 이동할 필요가 없습니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 최초 설정에만 복사합니다. 기존 .env는 덮어쓰지 않습니다.
test -f .env || cp .env.example .env
```

`.env`에 아래 환경변수를 설정한 후 실행합니다.

```bash
python -m uvicorn ai_service.main:app --host 0.0.0.0 --port 8000
```

- 상태 확인: `GET http://localhost:8000/health`
- Swagger UI: `http://localhost:8000/docs`
- OpenAPI Schema: `http://localhost:8000/openapi.json`

`/health`의 `{"status":"ok","mode":"live"}` 응답은 서버 프로세스 확인용입니다. 외부 API 키나 상품 권한의 정상 동작까지 검사하지는 않습니다.

## 환경변수

| 이름 | 설정 조건 | 용도 |
| --- | --- | --- |
| `AUDIGO_API_TOKEN` | 필수, 32자 이상 | 백엔드와 AI 서버가 공유하는 내부 Bearer 토큰 |
| `OPENAI_API_KEY` | 필수 또는 아래 별칭 사용 | OpenAI 호출 키 |
| `OPEN_API_KEY` | 기존 이름 호환용 | `OPENAI_API_KEY`가 없을 때 사용 |
| `OPENAI_MODEL` | 선택, 기본 `gpt-4o-mini` | 일정·음악 선택 모델 |
| `KAKAO_REST_API_KEY` | 필수 | Kakao Local 장소 검색 및 카카오맵 대중교통·도보 경로 조회 |

환경변수는 서버 파일 옆의 `.env`에서 읽으며, 운영체제에 주입한 환경변수가 우선합니다. `.env.example`의 `AUDIGO_MODE`와 카카오 JavaScript·Native 키는 현재 AI 서버의 동작 설정에 사용하지 않습니다.

실제 `.env`는 Git에 올리지 않고 배포 환경에서 별도로 주입합니다. 내부 토큰과 외부 API 키는 백엔드·서버에서 사용하며 프론트에 전달하지 않습니다.


## API

| 메서드 | 경로 | 응답 |
| --- | --- | --- |
| `GET` | `/health` | 서버 상태 JSON, 인증 없음 |
| `POST` | `/internal/ai/itineraries/generate` | 최종 여행 일정 JSON |
| `POST` | `/internal/ai/itineraries/generate/stream` | 장소·숙소·이동·음악 결과를 순차 전송하는 SSE |

두 생성 API에는 다음 헤더가 필요합니다.

```http
Authorization: Bearer <AUDIGO_API_TOKEN>
Content-Type: application/json
```

### 일정 생성 요청

아래 JSON을 `request.json`으로 저장해 사용할 수 있습니다. 필수 장소가 없는 요청 예시이며, 실제 요청에서는 사용자 선택값을 전달합니다.

```json
{
  "generation_job_id": 10,
  "region": {
    "region_id": 1,
    "full_name": "제주특별자치도 서귀포시"
  },
  "duration": {
    "arrival_datetime": "2026-10-10T13:00:00",
    "departure_datetime": "2026-10-12T18:00:00"
  },
  "headcount": 2,
  "companion_type": "COUPLE",
  "preference": {
    "pace_type": "RELAXED",
    "transport_type": "PUBLIC_TRANSPORT",
    "budget_min": 300000,
    "budget_max": 800000,
    "budget_currency": "KRW",
    "distance_preference": 70,
    "themes": ["NATURE", "FOOD"],
    "foods": ["KOREAN", "JAPANESE"],
    "extra_request": "너무 빡빡하지 않게 추천해주세요."
  },
  "required_places": []
}
```

```bash
# 호출하는 셸에도 AUDIGO_API_TOKEN을 설정한 후 실행합니다.
# 서버가 읽는 .env는 호출 셸의 환경변수를 자동으로 설정하지 않습니다.
curl --fail-with-body http://localhost:8000/internal/ai/itineraries/generate \
  -H "Authorization: Bearer ${AUDIGO_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

요청 필드와 제약은 [schemas.py](ai_service/schemas.py) 및 실행 중인 서버의 `/docs`를 기준으로 확인합니다.

- 이동수단의 기본 값은 `PUBLIC_TRANSPORT`, `WALK`, `CAR`입니다. 여행 속도는 `RELAXED`, `BALANCED`, `PACKED`를 사용합니다.
- 날짜는 ISO 형식이며 시간대가 없으면 한국 시간으로 처리합니다. 도착·출발 일시 모두 시간대를 지정하거나 모두 생략해야 합니다. 최대 8개 달력 날짜를 포함할 수 있습니다.
- `distance_preference`는 0이면 가까운 이동을 선호하고, 100이면 긴 이동도 허용하는 것으로 해석합니다.
- 필수 장소는 최대 64개이며, 각 항목은 `provider: "KAKAO"`, `provider_place_id`, **`place_name`**, `address`, `latitude`, `longitude`, `category`, `order`를 포함합니다. `road_address`는 생략할 수 있습니다.
- 필수 장소 ID와 `order`는 각각 중복할 수 없습니다. 백엔드는 카카오에서 확인한 일관된 ID·이름·좌표를 전달해야 합니다.
- 명세에 없는 요청 필드는 허용하지 않습니다. `title`과 `days`는 AI가 생성하는 응답 필드입니다.

### 최종 일정 응답

성공 시 일정 객체를 직접 반환합니다. 별도의 `data` 포장 필드는 없습니다.

| 필드 | 내용 |
| --- | --- |
| `generation_job_id` 및 여행 조건 | 요청과 결과를 연결하는 정보 |
| `required_places` | 필수 장소 정보. 이 응답 배열에서는 이름이 `name`이며 요청의 `place_name`과 구분 |
| `title` | AI가 생성한 여행 제목 |
| `days[].day_number`, `date` | 여행 일차와 날짜 |
| `days[].items[]` | 장소 정보, 좌표, `관광·식당·숙소` 카테고리, 방문 순서, 시작·종료 시각, 체류시간 |
| `days[].items[].travel_minutes_from_previous` | 직전 장소에서 이동하는 시간(분) |
| `days[].items[].route_from_previous` | 대중교통·도보 경로 상세. 이전 장소가 없는 첫 방문 등에서는 `null` |
| `timezone`, `model_version`, `warnings` | 시간대, 사용 모델, 데이터 한계 안내 |

`reason`과 `source_category`는 응답하지 않습니다. 일반 JSON API에는 음악 추천 결과가 포함되지 않습니다.

### 단계별 SSE 요청 및 응답

기존 일정 요청에 `music_candidates`를 추가하고 `/internal/ai/itineraries/generate/stream`을 호출합니다. 후보는 1~100개이며, 각 후보에는 `music_id`(양수·중복 금지), `title`, `artist`, `youtube_url`이 필요합니다. 백엔드의 실제 음악 후보를 보내야 하며, 모델은 그중 한 곡을 선택합니다.

전체 요청을 `stream-request.json`에 저장한 경우:

```bash
curl --no-buffer --fail-with-body http://localhost:8000/internal/ai/itineraries/generate/stream \
  -H "Authorization: Bearer ${AUDIGO_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  --data-binary @stream-request.json
```

단계는 **순차 실행**됩니다. 한 SSE 연결에서 완료 결과를 전달한 뒤 다음 단계를 실행합니다.

| 순서 | `stage` | `stage_completed.data` |
| --- | --- | --- |
| 1 | `PLACES` | `title`, 날짜별 관광·식당과 방문 순서가 있는 `days` |
| 2 | `ACCOMMODATIONS` | 날짜별 검색 중심 좌표와 숙소가 있는 `accommodations` |
| 3 | `ROUTES` | 최종 시간표 `itinerary`, 이동 구간 `routes` |
| 4 | `MUSIC` | 선택한 후보의 `music_id`, `title`, `artist`, `youtube_url` |

각 단계는 `stage_started`와 `stage_completed` 이벤트를 보냅니다. 전체 성공 시 마지막 `complete` 이벤트의 `data`에 `itinerary`, `routes`, `music`이 담깁니다. 공통 식별 정보는 `generation_job_id`, `request_id`입니다.

프론트는 `stage_completed`를 받은 뒤 해당 단계에 체크 표시하고, `complete`를 받으면 결과 화면으로 이동합니다. `progress`의 25/50/75/100은 완료한 단계의 비율입니다. 최초 장소 추천에는 최종 방문 시간이 없으므로 `ROUTES` 결과를 시간표에 사용합니다.

백엔드는 응답 전체가 끝나기 전에 빈 줄로 구분된 SSE 이벤트를 읽어 프론트로 중계해야 합니다. POST 요청이므로 브라우저의 기본 `EventSource`에 URL만 연결하는 방식으로 호출할 수 없습니다.

## 카카오맵 표시와 길찾기

카카오 장소·좌표와 카카오맵 REST API의 경로 결과를 반환합니다. 프론트는 카카오맵 SDK에서 이 좌표로 마커와 경로를 표시합니다. 별도 TMAP 키는 사용하지 않습니다.

- `provider: "KAKAO"`, `is_estimated: false`는 실제 카카오 경로 조회 결과를 뜻합니다. 도착 시각 보증은 아닙니다.
- `legs[]`에는 버스·지하철과 도보 구간이 순서대로 담깁니다. 대중교통 결과에서 빠진 승차 전·환승·하차 후 도보는 카카오 도보 API로 보완합니다.
- `bus_number`, `vehicles`, `start`, `end`, `stops`, `path`, `instructions`로 노선·정류장·지도 좌표·도보 안내를 표시합니다. 카카오가 좌표를 주지 않는 중간 정류장은 이름만 제공하며 좌표는 `null`입니다.
- `map_url`은 카카오맵 길찾기 화면으로 연결하는 URL입니다.
- 대중교통 검색 결과가 없으면 도보 경로를 조회하여 `walking_fallback: true`, `transit_available: false`, `message: "이용할 수 있는 대중교통이 없습니다"`를 반환합니다. API 오류를 대중교통 부재로 처리하지 않습니다.
- 카카오 명세에 여행 날짜·출발 시각을 지정하는 항목이 없어 **예정일의 운행·막차·심야버스 여부는 검증하지 않습니다**. `schedule_verified: false`, `service_checked_at: null`이며, `transit_available`은 조회된 경로의 유무를 뜻합니다. 출발 전 카카오맵에서 운행 여부를 확인해야 합니다.
- 조회한 소요시간을 여행 일정에 반영합니다. 시간 안에 필수 장소를 배치할 수 없으면 실패 처리합니다. 자동차는 기존 좌표 기반 추정을 유지합니다.

공식 규격: [카카오맵 REST API](https://developers.kakao.com/docs/ko/kakaomap/rest-api).

## 오류와 운영 설정

| HTTP 상태 | 대표 `message` | 의미 |
| --- | --- | --- |
| 400 | `invalid_request` | 요청 형식·필드·값 검증 실패 |
| 401 | `unauthorized` | 내부 인증 토큰 누락 또는 불일치 |
| 422 | `ai_itinerary_generation_failed` | 필수 장소·시간 등 조건을 만족하는 일정 생성 실패 |
| 422 | `ai_music_recommendation_failed` | 음악 후보 선택 실패 |
| 503 | `ai_service_unavailable` | 설정 누락, 모델·장소 API 장애, 처리 제한 시간 초과 등 |
| 503 | `routing_service_unavailable` | 길찾기 설정·조회·응답 검증 실패 |
| 500 | `internal_server_error` | 예상하지 못한 내부 오류 |

일반 오류는 `message`, `data`를 포함한 JSON으로 반환합니다. SSE가 시작된 뒤에는 HTTP 200이 이미 전송되므로 **`error` 이벤트가 최종 실패 신호**입니다. 이벤트의 실패 단계와 `data.http_status`, `data.error_message`를 확인해야 하며, 이후 단계와 `complete`는 전송되지 않습니다.

- 일반 생성·SSE 전체 제한: **각 300초(5분)**. 생성이 5분을 초과하면 실패로 처리합니다. SSE는 장소·숙소·이동·음악 전 단계를 합한 제한이며 단계마다 초기화하지 않습니다.
- 모델 호출마다 60초 / 카카오 장소·경로 호출마다 10초
- SSE 대기 중 10초마다 `: keep-alive` 주석 전송
- 백엔드와 프록시의 응답 버퍼링을 끄고, AI의 300초 처리 후 실패 응답까지 전달할 수 있도록 연결 제한 시간에 여유 설정
- 연결이 끊기면 진행 중 처리를 취소하며, 재시도는 새 생성 요청으로 처리
- `generation_job_id`는 요청 식별값으로 사용하며 작업 조회·중복 실행 방지·이벤트 재생은 제공하지 않음

제한 시간은 현재 [config.py](ai_service/config.py)의 기본값입니다. 서버는 `X-Request-Id` 응답 헤더를 제공하며, SSE 이벤트에도 `request_id`를 포함합니다.

## 파일 구조와 배포 범위

```text
.
├── README.md
├── API_TERMS.md    # 응답 필드·좌표·화면 연결 설명
├── .env.example
├── requirements.txt
└── ai_service/
    ├── __init__.py
    ├── main.py        # API 진입점 및 공통 응답 처리
    ├── auth.py        # 내부 Bearer 인증
    ├── config.py      # 환경 설정
    ├── errors.py      # 오류 정의
    ├── schemas.py     # 요청·응답 형식
    ├── places.py      # 카카오 장소 후보
    ├── model.py       # OpenAI 일정·음악 선택
    ├── features.py    # 일정 구성·검증
    ├── pipeline.py    # 단계별 처리 순서
    ├── routing.py     # 카카오 경로 조회 및 시간표 반영
    └── streaming.py   # SSE 전송·오류 종료
```

실행에는 `ai_service/`, `requirements.txt`와 배포 환경의 설정이 필요합니다. `uv` 설치는 필요하지 않습니다. README와 `.env.example`은 레포지토리에 함께 포함하는 안내 자료입니다. `.env`, `.venv`, 캐시, 개인 개발 기록은 배포 이미지에 포함하지 않습니다. 별도 AI 레포지토리에도 `.env`·가상환경·캐시를 제외하는 `.gitignore`를 적용해야 합니다.

테스트는 서버 실행에 필수인 파일이 아니므로 배포 이미지에서 제외할 수 있습니다. 현재 이 폴더에는 테스트 디렉터리가 포함되어 있지 않습니다.
