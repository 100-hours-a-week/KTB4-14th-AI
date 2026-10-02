# AUDIGO AI 로컬 실행 — 한 번에 붙여넣기

아래 블록 전체를 **터미널에 한 번에 복사해서 붙여넣으면 설치부터 로컬 서버 실행까지 진행됩니다.** `uv`가 잠금 파일에 맞춰 가상환경을 만들거나 갱신합니다. 기존 `.env`와 API 키는 그대로 유지합니다.

현재 Mac의 Python 3.13(`/opt/homebrew/bin/python3.13`)과 이 저장소의 경로를 기준으로 작성했습니다.

```bash
(
  set -e
  cd /Users/samrobert/Documents/GitHub/KTB4-14th-AI
  unset PYTHONHOME PYTHONPATH

  uv sync --frozen --no-dev --python /opt/homebrew/bin/python3.13
  ./.venv/bin/python scripts/download_e5_model.py model

  if [ ! -f .env ]; then
    cp .env.example .env
  fi

  ./.venv/bin/python -m uvicorn ai_service.main:app --host 127.0.0.1 --port 8000 --reload
)
```

정상 실행되면 마지막에 다음과 비슷하게 표시됩니다.

```text
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

서버가 실행되면 브라우저에서 [API 테스트 화면](http://localhost:8000/docs)을 엽니다.

- 서버 상태: [http://localhost:8000/health](http://localhost:8000/health)
- API 명세: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

## 설치가 끝난 뒤 빠르게 다시 실행하기

이미 위 설치를 한 번 완료했다면 다음부터는 아래 블록만 붙여넣으면 됩니다.

```bash
(
  set -e
  cd /Users/samrobert/Documents/GitHub/KTB4-14th-AI
  unset PYTHONHOME PYTHONPATH
  ./.venv/bin/python -m uvicorn ai_service.main:app --host 127.0.0.1 --port 8000 --reload
)
```

## 참고

- 명령어에 `→`, `%`, 프롬프트 문자열은 붙여넣지 않습니다.
- `ai_service.main:app`에는 백슬래시를 넣지 않습니다.
- 서버 실행 명령을 실행하면 해당 터미널은 Uvicorn 서버 로그를 표시하는 상태로 유지됩니다.
- 서버를 종료할 때는 `Ctrl+C`를 누릅니다.
- `--reload`로 실행하므로 Python 코드를 저장하면 서버가 자동으로 다시 시작됩니다.
- `.env`가 이미 있으면 덮어쓰지 않습니다.
- 처음 실행해서 `.env.example`로 `.env`를 만든 경우에는 본인 API 키를 설정해야 실제 일정 생성이 가능합니다. `/health` 성공은 외부 API 키 검증을 의미하지 않습니다.
- `Address already in use`가 나오면 8000번 포트에 서버가 이미 실행 중입니다. 기존 서버를 사용하거나, 그 서버를 실행한 터미널에서 `Ctrl+C`로 종료한 뒤 다시 실행하세요.

## 식당 영업시간 확인

카카오 장소 검색에는 영업시간 필드가 없습니다. 제주 식당 다섯 곳은 [확인한 영업시간 데이터](ai_service/data/restaurant_hours.jeju.json)에 포함되어 제주 일정 생성 시 자동 적용됩니다. 네이버 지도에서 **장소명과 주소를 함께 대조한 뒤** 카카오 장소 ID에 연결했습니다. 라스트오더가 있는 곳은 영업시간 종료보다 앞선 라스트오더 시각까지 방문이 끝나도록 보수적으로 입력했습니다. 예시 파일의 ID와 시간은 실제 장소 데이터가 아닙니다.

다른 지역에 적용하거나 제주 식당 목록을 늘리려면 `restaurant_hours.json.example` 형식의 JSON을 만들고 운영 서버의 `.env`에 `RESTAURANT_HOURS_FILE`을 그 파일의 절대 경로로 설정하세요. 파일을 설정하면 기록이 없는 선택 식당은 후보에서 빠지고, 필수 식당의 시간이 없으면 422로 중단합니다. 일정 생성은 식당 체류 **시작부터 끝까지** 영업시간 안에 들어가도록 기다리거나 다른 식당으로 교체하며, 실제 이동 경로 반영 후 다시 확인합니다. 맞는 식당이 없으면 방문을 제외하고, 필수 방문 및 최소 일정 조건 때문에 제외할 수 없다면 422를 반환합니다. 제주 외 지역은 별도 파일을 설정하지 않으면 기존 동작이며 영업시간이 확인된 것으로 간주하지 않습니다. 네이버 지도 정보 자체가 바뀌거나 공휴일 임시휴무가 생길 수 있으므로 운영 데이터는 주기적으로 갱신해야 합니다.

## 일정 생성 JSON 1,000건 재현 테스트

```bash
./.venv/bin/python -m unittest discover -s tests -p test_http_request_stress.py -q
```

이 테스트는 고정 난수 시드 `20261002`로 `/internal/ai/itineraries/generate`에 JSON 1,000건을 FastAPI `TestClient`로 전송합니다. 날짜·도착/출발 시각·여행 속도·인원·예산·취향·필수 장소를 바꿉니다. 카카오 장소·OpenAI 선택과 음악 응답은 테스트용으로 고정하고 외부 HTTP 요청은 차단합니다. 실제 일정 생성·숙소 배치·경로 시간표·응답 직렬화 코드는 실행합니다. 800건은 성공 가능한 조건으로 200과 4단계 완료를 검사하고, 200건은 잘못된 요청 또는 충족 불가능한 조건으로 예상 400/422를 검사합니다. 성공 응답에 식당 영업시간이나 음악 필드가 없는지도 확인합니다. 이는 운영 외부 API의 가용성이나 실제 추천 품질을 측정하는 테스트는 아닙니다.

성공 가능한 요청만 1,000건 보내서 422 발생 여부를 확인하려면 다음 명령을 실행하세요. 고정 시드 `20261003`을 사용하며 결과에 `status_counts={200: 1000}`이 표시되어야 합니다.

```bash
./.venv/bin/python tests/test_http_request_stress.py HttpRequestStressTests.test_1000_feasible_json_requests -q
```

전국 17개 시·도 대표 지역을 순회하는 1,000건 테스트는 아래 명령으로 실행합니다. 각 지역의 장소·좌표·영업시간은 **테스트용 가상 데이터**이고 외부 API 호출은 없습니다. 일정 배치 코드가 지역명별 요청에서 422 없이 동작하는지 확인하는 용도이며, 전국 실제 식당의 영업시간 확인이나 운영 성공률을 보증하지 않습니다.

```bash
./.venv/bin/python tests/test_nationwide_http_stress.py -q
```

## 현재 요청과 검증

**여행 요청은 두 형식을 지원합니다.** Swagger에는 사용자가 확정한 요청 예시 하나만 표시합니다. 표시 형식은 `generation_job_id`, `region`, `duration`, `budget_type`, 장소 `category` 형식입니다. JSON을 그대로 붙여 넣을 수 있습니다. 백엔드 `AiTravelGenerationRequest.java`의 최상위 `travel_plan_id`, `region_id`, `region_name`, 날짜 형식도 계속 지원합니다. 두 형식을 섞지는 않습니다. 사용자 중첩 요청에는 `generation_job_id`만 보내면 되며 `travel_plan_id`는 필요하지 않습니다. 사용하지 않는 ID는 응답에서 생략합니다.

| 용도 | POST 경로 |
| --- | --- |
| 백엔드 여행 일정 생성(SSE) | `/api/ai/v1/itinerary-jobs/stream` |
| 백엔드 여행 일정 생성(JSON) | `/internal/ai/itineraries/generate` |
| 여행 정보 기반 음악 추천 | `/internal/ai/music/recommend` |

- 백엔드 DTO 형식 요청: `travel_plan_id`, `region_id`, `region_name`, `arrival_datetime`, `departure_datetime`, `headcount`, `companion_type`, `preference`, `required_places`.
- 백엔드 DTO 형식은 `budget_type`, `place_type`을 사용합니다. 사용자 중첩 형식은 `budget_type`, `category`, `road_address`를 받습니다. `client_draft_id`는 받지 않습니다. 작업 ID를 여행 ID로 바꾸지 않습니다.
- 지역명은 백엔드 형식의 `region_name` 또는 중첩 형식의 `region.full_name`을 사용합니다. 별도 지역 목록 파일이 필요하지 않습니다.
- 날짜는 한국시간 `2026-09-19T10:00:00` 형식으로 보낼 수 있습니다.
- JSON 일정 생성은 장소 추천 → 숙소 추천 → 경로 최적화 → 음악 추천을 모두 마친 뒤 기존 `ItineraryResponse` JSON을 반환합니다. 음악 추천 결과는 내부 파이프라인 결과에 포함되지만 기존 응답 스키마에는 음악 필드가 없습니다. 음악을 클라이언트에 전달하려면 별도 `/internal/ai/music/recommend` API를 사용합니다.
- 이동 안내는 기존 응답의 `days[].items[].route_from_previous`에 포함됩니다. `transport_type`, `duration_minutes`, `distance_meter`, 문자열 `line_name`·`vehicle_number`, 전체 경로 요금 `total_fare_amount`, `legs`를 반환합니다. 각 구간은 `mode`, 문자열 `line_name`·`vehicle_number`, `start`·`end`(이름·정류장 번호)만 포함합니다. 없는 문자열은 null, 자동차 구간은 빈 배열입니다. 요금이 확인되지 않으면 null, 도보는 0, 자동차는 null입니다. 구간별 시간/거리·상세 좌표는 내부에서만 사용합니다.
- 모든 기능 호출에는 `Authorization: Bearer <기존 서비스 토큰>`이 필요합니다.
- 별도 음악 API는 `travel_plan_id`, `region`, `duration`, `preference`를 받습니다. 여행 시작일의 한국 시간 월에 따라 봄(3~5월)·여름(6~8월)·가을(9~11월)·겨울(12~2월)을 정합니다. YouTube 실시간 검색에 계절을 넣고, 제목에 다른 계절이 명시된 곡은 제외합니다. 남은 곡명·가수 후보를 로컬 `multilingual-e5-small` 모델로 정렬하며 계절이 명시적으로 맞는 곡을 우대하고, 공개 메타데이터로 영상을 검증합니다. 이 API는 OpenAI 키가 필요하지 않습니다. 후보가 없거나 검증에 모두 실패하면 422, 검색·모델 장애는 503을 반환합니다.
- 일정 생성의 내부 음악 단계는 기존 OpenAI 기반 추천을 사용합니다. JSON 일정 응답에는 음악 필드가 없으며 SSE 최종 `result.music`에는 음악이 포함됩니다.
- 공개 검색에는 `yt-dlp`를 사용합니다(`requirements.txt`에 포함). 영상·음원 파일은 다운로드하지 않습니다. 검색 차단·페이지 변경 시 실패할 수 있으며, 실패를 검색 페이지 링크로 대체하지 않습니다. [음악 링크 변경 내용](../문서/YouTube_음악영상_링크_변경.md)을 참고하세요.
- E5 ONNX 모델과 토크나이저는 고정 리비전과 SHA-256 검증으로 배포 이미지에 포함됩니다. 위 로컬 실행 명령도 모델을 내려받고 검증합니다. 모델은 요청 중 다운로드하지 않으며 기본 경로는 `model/`입니다. 필요하면 `E5_MODEL_DIR`로 경로를 지정할 수 있습니다. YouTube 제목·채널만으로는 곡 분위기를 충분히 알기 어려우므로 추천 품질은 실제 결과로 점검해야 합니다.

### 백엔드 SSE 연결

백엔드 환경변수는 AI 서버 주소와 복구된 경로로 설정합니다. 아래 포트는 AI를 8000으로 실행하는 경우입니다.

```ini
AUDIGO_AI_BASE_URL=http://127.0.0.1:8000
AUDIGO_AI_SSE_PATH=/api/ai/v1/itinerary-jobs/stream
```

AI와 백엔드의 `AUDIGO_API_TOKEN`은 같은 값을 사용합니다. 요청 JSON은 위의 두 형식을 모두 지원합니다. `/docs`의 요청 예시를 `request.json`으로 저장하고 여행 날짜를 수정한 뒤, 토큰이 설정된 터미널에서 실행합니다.

```bash
curl --no-buffer --fail-with-body \
  http://127.0.0.1:8000/api/ai/v1/itinerary-jobs/stream \
  -H "Authorization: Bearer ${AUDIGO_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  --data-binary @request.json
```

성공 시 이벤트 순서는 다음과 같습니다.

```text
PLACE_RECOMMEND_STARTED → PLACE_RECOMMEND_DONE
STAY_RECOMMEND_STARTED → STAY_RECOMMEND_DONE
ROUTE_OPTIMIZE_STARTED
MUSIC_RECOMMEND_STARTED → MUSIC_RECOMMEND_DONE
ROUTE_OPTIMIZE_DONE → complete
```

- `ROUTE_OPTIMIZE_DONE.result`에 `title`, `days`, `music`을 한 번만 보냅니다. 백엔드가 이 이벤트를 받으면 저장하므로 음악 처리까지 끝난 뒤 보냅니다. 음악 API 오류에는 기존 대체곡 정책을 적용합니다.
- `days[].items`는 `place_type`을 사용하며 관광은 `TOURISM`입니다. 같은 날짜의 이동은 `days[].routes`에 `from_sequence`, `to_sequence`, `order`와 최신 간결한 경로 필드를 보냅니다. 전날 마지막 장소에서의 이동은 다음날 첫 장소의 `route_from_previous`에 보존합니다.
- 최종 결과와 `complete`, `error`에는 요청에 맞는 `travel_plan_id` 또는 `generation_job_id`를 유지합니다. `complete`에는 결과를 중복하지 않습니다.
- 인증·요청 검증·키 설정 오류는 스트림 시작 전에 HTTP JSON 오류를 반환합니다. HTTP 200 이후 실패는 `event: error`로 끝나며 `ROUTE_OPTIMIZE_DONE`과 `complete`를 보내지 않습니다. 클라이언트는 HTTP 상태만으로 성공을 판단하면 안 됩니다.
- 기본 전체 제한 시간은 300초, 유휴 하트비트는 15초(`: keep-alive`)입니다. 연결이 종료되면 진행 중인 생성 작업도 취소합니다. SSE 재연결 시 작업 재개나 `Last-Event-ID` 재생은 지원하지 않습니다.

### 확인할 Python 파일

| 파일 | 역할 |
| --- | --- |
| `ai_service/schemas.py` | 백엔드 요청 필드 검증·내부 변환 |
| `ai_service/main.py` | API 경로·인증·JSON/SSE 응답 |
| `ai_service/streaming.py` | SSE 하트비트·제한 시간·오류·연결 종료 처리 |
| `ai_service/backend_contract.py` | 백엔드 단계 이벤트·저장용 결과 변환 |
| `ai_service/pipeline.py` | 장소·숙소·경로·음악 공통 생성 흐름 |
| `ai_service/e5_music.py` | 별도 음악 API의 YouTube 후보 선택·E5 정렬 |
| `ai_service/api_examples.py` | Swagger 요청 예시 |

[복사용 여행 요청](../문서/참고자료/여행일정생성요청.json) · [백엔드 DTO 요청](../문서/참고자료/백엔드_여행일정생성요청.json) · [현재 API 규격](../문서/API_TERMS.md) · [백엔드에 전달할 사항](../문서/백엔드_AI_연동_반영사항.md)

실행 가능한 회귀 테스트는 `tests/`에 유지합니다. 다음 명령은 별도 터미널에서 실행하며 실제 외부 모델·카카오 API를 호출하지 않습니다.

```bash
cd /Users/samrobert/Documents/GitHub/KTB4-14th-AI
./.venv/bin/python -m unittest discover -s tests -v
```

### 4단계 적용 확인

`./.venv/bin/python -m unittest discover -s tests -p test_backend_contract.py -v`는 외부 API 없이 생성 단계의 시작·완료 로그가 `PLACE_RECOMMEND → STAY_RECOMMEND → ROUTE_OPTIMIZE → MUSIC_RECOMMEND` 순서인지 확인합니다. 기존 JSON 응답과 두 요청 형식의 SSE 이벤트 순서·최종 결과·인증·실패 처리를 검사합니다. `test_streaming.py`는 실제 생성 완료 전 이벤트 전송, 하트비트, 시간 초과, 연결 종료 시 작업 취소를 검사합니다.

실제 API로 확인할 때는 **이 저장소 경로에서** 서버를 실행한 뒤 `/docs`의 `POST /internal/ai/itineraries/generate`에 유효한 요청과 Bearer 토큰을 넣습니다. 예시의 여행 날짜는 테스트할 날짜로 바꿉니다. 응답 헤더 `X-Request-Id`와 같은 `request_id`로 서버 로그의 `generation_stage` 8건(각 단계 `STARTED`, `COMPLETED`)을 확인합니다. 네 단계가 끝나면 `application/json` 응답이 반환됩니다. `/openapi.json`에는 JSON과 SSE 경로가 함께 표시됩니다.

## 요청에서 400이 발생할 때

응답의 `data.error_message`에서 누락된 필드나 JSON 문법 오류를 확인하세요. Swagger의 사용자 중첩 요청 예시는 `generation_job_id`를 받습니다. 문서에서 복사한 줄 끝의 역슬래시, `<br>` 같은 표기 문자는 JSON에 넣지 않습니다.

## 실제 일정 생성 검증

`tests/`는 외부 API를 모의 응답으로 대체하는 회귀 테스트입니다. 실제 생성 검증은 서버를 실행한 뒤 별도 터미널에서 아래 명령으로 진행합니다. **실제 외부 API 호출 비용이 발생합니다.** 키는 기존 `.env`에서 읽으며 출력하지 않습니다. 실행 중인 서버와 같은 인증 설정을 사용해야 합니다.

```bash
cd /Users/samrobert/Documents/GitHub/KTB4-14th-AI
./.venv/bin/python scripts/check_generation_live.py --live --base-url http://127.0.0.1:8000
```

서울·부산 반복 요청, 강릉·제주, 일반 JSON, 시간 부족·잘못된 요청·장소 상세 누락을 검사합니다. 실패하면 종료 코드 1을 반환합니다. 결과는 기본 `/tmp/audigo-generation-e2e.json`에 저장됩니다. 프론트·백엔드 저장까지의 전체 앱 테스트는 별도로 필요합니다.

[실패 원인·수정 내용·실검증 기록](../문서/여행생성_실패_수정_검증.md)

밤 일정은 18:00~23:59(한국시간) 안에서 생성합니다. 18시 이후 도착하면 도착일 장소 선택에도 이 범위를 적용합니다. `preference.extra_request`에 “밤 일정만 만들어주세요” 또는 “야간 일정만 만들어주세요”를 넣으면 여행의 모든 날짜에 적용합니다. 실제 도착/출발 시각과 도착 후 준비시간을 함께 반영하며, 다음날 00:00~05:59로 방문을 넘기지 않습니다. 일반 주간 여행의 시간 규칙은 유지합니다.
