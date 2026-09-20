# AUDIGO AI 로컬 실행 — 한 번에 붙여넣기

아래 블록 전체를 **터미널에 한 번에 복사해서 붙여넣으면 설치부터 로컬 서버 실행까지 진행됩니다.** 처음 실행할 때만 가상환경을 만들고, 이후에는 재사용합니다. 기존 `.env`와 API 키는 그대로 유지합니다.

현재 Mac의 Python 3.13(`/opt/homebrew/bin/python3.13`)과 프로젝트 경로를 기준으로 작성했습니다.

```bash
(
  set -e
  cd /Users/samrobert/Documents/GitHub/AI-parking-assignment/development
  unset PYTHONHOME PYTHONPATH

  if [ ! -x .venv/bin/python ]; then
    /opt/homebrew/bin/python3.13 -m venv .venv
  fi

  ./.venv/bin/python -I -m pip install -r requirements.txt

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
  cd /Users/samrobert/Documents/GitHub/AI-parking-assignment/development
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

## 현재 요청과 검증

스프레드시트의 두 요청은 아래 API에 각각 보냅니다. JSON 파일 내용을 Swagger의 Request body에 복사할 수 있습니다.

| 요청 | POST 경로 | 복사용 파일 |
| --- | --- | --- |
| 여행 조건 | `/internal/ai/itineraries/generate` 또는 `/internal/ai/itineraries/generate/stream` | [여행 요청](../문서/참고자료/스프레드시트_여행요청.json) |
| 음악 후보 | `/internal/ai/music/recommend` | [음악 요청](../문서/참고자료/스프레드시트_음악요청.json) |

- 여행 요청과 응답 모두 `budget_currency`를 사용합니다. `budget_type`으로 바꾸지 않습니다. 요청의 `client_draft_id`도 일반 생성 응답과 스트림 최종 `data.itinerary`에 그대로 반환합니다. 생략하면 `null`입니다.
- 음악 API는 전달된 `candidates` 중 한 곡을 선택하고 `travel_plan_id`, `music_id`, `title`, `artist`, `youtube_url`을 `message`, `data` 응답으로 반환합니다. 후보가 한 곡이면 그대로 반환합니다. 예시 URL의 `v=example`은 자리표시자이므로 실제 후보 URL로 교체하세요.
- 음악 후보 요청은 여행 생성·스트림 API에 보내지 않습니다. 아래 스트림의 기존 음악 추천 방식과 별도입니다.

- 음악까지 받으려면 `/internal/ai/itineraries/generate/stream`을 사용합니다. `music_candidates` 없이 기존 여행 조건만 보냅니다.
- SSE 중간 네 단계는 `stage`, `status`만 전달합니다. 일정·음악은 마지막 `event: complete`의 `data.itinerary`, `data.music`에서 한 번만 받습니다. 기존 `error` 이벤트 형식은 유지합니다.
- 스트림의 음악은 고정 후보 없이 추천한 곡을 공개 카탈로그에서 확인합니다. `title`, `artist`, `youtube_url`만 반환하며 URL은 추가 키가 필요 없는 YouTube 검색 링크입니다. 스트림 음악에는 `music_id`가 없습니다.
- `extra_request`의 `1일차 렌터카, 2~3일차 대중교통`은 날짜별 실제 경로 계산에 적용합니다. CAR 시간·거리는 기존 좌표 기반 추정입니다.
- 한국시간은 `2026-09-19T10:00:00`으로 입력할 수 있습니다. `Z`나 `+09:00`을 붙일 필요가 없습니다.
- 날짜는 ERD의 `travel_date`를 사용합니다. 이동정보는 각 항목의 `route_from_previous`에 이동수단·시간·거리만 반환하며, 별도 `routes` 목록은 없습니다. 출발·도착 좌표는 이전·현재 방문 항목에서 읽습니다.
- [현재 API 규격](../문서/API_TERMS.md), [실제 필드 삭제·통합 내역](../문서/AI_API_ERD_적용내역.md)

실행 가능한 회귀 테스트는 `tests/`에 유지합니다. 다음 명령은 별도 터미널에서 실행하며 실제 외부 모델·카카오 API를 호출하지 않습니다.

```bash
cd /Users/samrobert/Documents/GitHub/AI-parking-assignment/development
./.venv/bin/python -m unittest discover -s tests -v
```
