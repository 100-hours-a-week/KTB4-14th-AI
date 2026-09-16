# Audigo AI API

**모든 명령은 이 `development/` 폴더에서 실행합니다.** 먼저 [V1 개발 가이드](V1%20개발%20가이드.md)를 확인하세요.

Google Sheets의 `[AUDIGO] AI 인공지능` 탭에 적힌 URL·요청·응답을 기준으로 만든 개발용 API다.

## 풀스택 팀에 전달할 값

풀스택 팀에는 아래 두 값만 전달한다.

```text
AI_API_BASE_URL=https://<Modal 배포 후 생성되는 URL>
AI_API_TOKEN=<AI 서비스 호출용 Bearer 토큰>
```

호출할 때는 다음 Header를 사용한다.

```http
Authorization: Bearer <AI_API_TOKEN>
Content-Type: application/json
```

일정 생성 요청에는 명세대로 `Idempotency-Key`도 함께 보낸다. `Idempotency-Key`는 인증 키가 아니라 같은 일정이 중복 생성되는 것을 막는 요청 식별자다.

Gemini 키와 Modal 계정 토큰은 풀스택 팀에 전달하지 않는다. 두 값은 AI 실행 환경의 Secret으로 보관한다. 기존 결정대로 `X-API-Key` Header는 사용하지 않는다.

## 현재 구현 범위

| API | 현재 상태 | 다음 연결 |
|---|---|---|
| 일정 작업 접수·조회 | API·검증·멱등 처리 동작, demo 결과 동작 | Gemini 연결 가능, 장소·영업·이동 데이터 공급자는 미연결 |
| 음악 추천 | 작은 고정 카탈로그의 규칙 기준선 동작 | 실제 곡 카탈로그 확보 후 E5 비교 |
| 동행자 추천 | 입력 후보의 테마·속도 비교 동작 | 후보 성별·예산 필드 계약 확정 필요 |
| 체크리스트 | 규칙 기준선 동작 | Qwen 모델 비교·Modal GPU 함수 연결 |
| 영상 작업 접수·조회 | 접수와 실패 상태 확인 가능 | LTX·FFmpeg·결과 파일 저장소 미연결 |
| 공통 오류 | `error`·`request_id` 형식 동작 | 팀 통합 테스트 |

`demo` 결과는 API 연동 확인용이며 실제 AI 추천 결과가 아니다. 영상은 성공한 것처럼 가짜 URL을 반환하지 않고 `FEATURE_NOT_CONFIGURED` 실패 상태를 반환한다.

## 로컬 실행

Python 3.11~3.14와 `uv`를 사용한다.

```sh
# .env가 없는 경우에만 복사합니다. 기존 토큰을 덮어쓰지 않습니다.
[ -f .env ] || cp .env.example .env
uv sync --frozen --extra dev --extra modal
```

`.env`의 `AUDIGO_API_TOKEN`에는 32바이트 이상의 임의 문자열을 넣는다. 토큰은 다음처럼 생성할 수 있다.

```sh
openssl rand -hex 32
```

환경변수를 읽어 실행한다.

```sh
set -a
source .env
set +a
uv run uvicorn ai_service.main:app --reload
```

- API 문서: `http://127.0.0.1:8000/docs`
- 상태 확인: `http://127.0.0.1:8000/health`

## 자동 테스트

```sh
uv run --extra dev pytest
```

자동 테스트는 요청 검증, Bearer 인증, 멱등 처리, 정상 응답, 공통 오류와 비동기 상태 조회를 확인한다. 모델 품질·실제 장소 정확성·운영 부하를 검증하는 테스트는 아니다.

## Modal 개발 설정

### 현재 개발 배포

- API 주소: https://samdwich0725--audigo-ai-api-web.modal.run
- [API 문서](https://samdwich0725--audigo-ai-api-web.modal.run/docs)
- [Modal 배포 관리](https://modal.com/apps/samdwich0725/main/deployed/audigo-ai-api)
- 실행 모드: `live`, 일정 모델: `gemini-3.5-flash-lite`
- 백엔드 전달 값: 로컬 `.env.backend` (서비스 토큰 포함, Git 제외)

2026-09-15 확인: 새 개발 폴더의 기존 테스트 11개 통과. 배포 주소에서 health·문서 200,
인증 누락 401, 음악 추천 200, 일정 접수 202, 동일 요청의 작업 ID 재사용,
Gemini 작업 SUCCEEDED 및 결과 조회를 확인했습니다. 음악은 데모 곡을 반환합니다.
이 확인은 연동 검증이며 실제 장소 정확성·추천 품질·부하 검증은 아닙니다.
Modal 이미지 빌드와 배포는 성공했습니다. 로컬 Docker 빌드는 Docker 데몬이 실행되지 않아 수행하지 못했습니다.

### CPU·메모리 설정

`modal_app.py`의 `V1_RESOURCES`와 각 함수의 설정으로 관리합니다. 대시보드에서 CPU를 따로 고를 필요가 없습니다.

| 설정 | API 서버 `web` | 일정 작업 `run_job` |
|---|---|---|
| CPU 요청 / 제한 | 0.25 / 1 물리 코어 | 0.25 / 1 물리 코어 |
| 메모리 요청 / 제한 | 256 / 512 MiB | 256 / 512 MiB |
| GPU | 없음 | 없음 |
| 최소 / 최대 컨테이너 | 0 / 1 | 0 / 2 |
| 컨테이너당 동시 요청 | 최대 10 | 1 |
| 유휴 종료 설정 | 60초 | 60초 |
| 실행 제한 시간 | 60초 | 120초 |
| 작업 자동 재시도 | 해당 없음 | 0회 |

음악은 작은 목록을 비교하고 Gemini 추론은 Google 서버에서 실행되므로 V1은 CPU로 시작합니다.
CPU·메모리 튜플은 `(요청량, 제한량)`이며 컨테이너별 값입니다. 첫 요청은 컨테이너 시작 때문에 느릴 수 있습니다.
최대 컨테이너 수는 동시 실행 규모를 제한하며 월 비용이나 Gemini 호출 횟수를 제한하지 않습니다.
이 자원은 V1 개발용입니다. 이후 음악 임베딩 모델·영상 모델 도입 시 실제 메모리와 지연을 측정해서 별도로 설정합니다.

### Secret 및 배포

Modal Secret `audigo-ai-secrets`에 다음 이름으로 등록합니다.

- `AUDIGO_API_TOKEN`: 32자 이상인 AI 서버 호출용 토큰
- `AUDIGO_MODE`: `demo` 또는 `live`
- `GEMINI_API_KEY`: Gemini 호출용 키 (`live`에서 필요)
- `GEMINI_ITINERARY_MODEL`: `gemini-3.5-flash-lite`

키 값은 소스에 쓰거나 셸 명령 인자로 넣지 말고 Modal 대시보드 Secret 편집 화면에서 입력합니다.
Secret 값을 바꾼 뒤에는 다시 배포하여 실행 중인 컨테이너에도 적용합니다.

```sh
# 로그인되지 않은 개발 환경에서만 실행
uv run --extra modal modal setup

# 이 폴더에서 배포
uv run --extra modal modal deploy modal_app.py
```

로컬 `.env`는 demo 개발용입니다. Modal은 별도 Secret을 읽습니다.
백엔드 전달용 `.env.backend`가 생성된 환경에서는 그 파일에 배포 주소와 서비스 토큰이 들어 있습니다.
`.env`와 `.env.backend`는 Git과 Docker 빌드에서 제외합니다. Gemini 키는 백엔드 전달 파일에 넣지 않습니다.

- [Modal CPU·메모리 설정](https://modal.com/docs/guide/resources)
- [Modal 자동 확장](https://modal.com/docs/guide/scale)
- [Modal Secret](https://modal.com/docs/guide/secrets)

## 후속 버전을 포함한 기존 2개월 로드맵

V1 당장 할 일은 [V1 개발 가이드](V1%20개발%20가이드.md)의 순서를 따릅니다.

1. **1주차:** 현재 API 계약, 인증, 정상·오류 응답을 demo 모드로 통합 확인한다.
2. **2주차:** Gemini Flash-Lite·Flash를 같은 일정 입력으로 비교하고 일정 모델을 결정한다.
3. **3~4주차:** 실제 장소·영업시간·이동시간 데이터 입력 계약을 정하고 일정 결과 검증을 붙인다.
4. **5주차:** 음악 카탈로그를 정한 뒤 규칙 기준선과 E5를 비교한다.
5. **6주차:** Qwen 체크리스트의 품질·지연·메모리를 측정하고 Modal 함수로 분리한다.
6. **7주차:** LTX 영상의 짧은 입력 시험과 FFmpeg 연결, 파일 저장 방식을 검증한다.
7. **8주차:** 전체 기능의 실패 처리·비용·호출량을 점검하고 36만 원 예산 안에서 제한값을 확정한다.

36만 원은 처음부터 모두 사용하지 않는다. 기능별 소규모 시험의 실제 사용량을 기록한 뒤, Gemini 호출비와 Modal CPU·GPU 비용을 나누어 다음 단계의 상한을 정한다.

## 현재 명세에서 합의가 필요한 값

- 일정 요청에는 후보 장소·영업시간·이동시간이 없어 실제 경로 정확성을 보장할 수 없다.
- 음악 요청에는 비교할 곡 목록 또는 카탈로그 출처가 없다.
- 동행자 후보에는 성별·예산이 없어 요청자의 성별·예산 조건을 적용할 수 없다.
- 작업 조회 응답에서 매칭·영상의 최종 `result` 상세 구조가 확정되지 않았다.
- 영상 결과 파일을 저장하고 접근 가능한 URL로 바꿀 저장소가 정해지지 않았다.

이 값들은 AI가 임의로 만들지 않고, 각 기능을 개발하기 전에 공유 API 명세에서 확정한다.
