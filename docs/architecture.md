# 프로젝트 구조와 코드 찾는 방법

코드의 역할을 기준으로 폴더를 나눕니다. 요청·응답 모델은 `schemas/`, 모델 지시문은 `prompts/`, 모델 요청과 결과 검증은 `planners/`, 실제 기능 처리는 `services/`, 외부 통신은 `clients/`에서 찾습니다.

```text
ai_service/
├── main.py                     # 앱 조립·라우터 등록
├── schemas/
│   ├── common.py               # 공통 모델·지역·기간·오류
│   ├── travel.py               # 여행 요청·응답·장소·경로·모델 출력
│   ├── music.py                # 음악 요청·응답·곡 정보·모델 출력
│   ├── matching.py             # 매칭 조건·후보·요청·추천 응답
│   └── jobs.py                 # 큐 접수·상태·오류 계약
├── prompts/
│   ├── travel.py               # 일정 지시문·인원/단계 조건·재시도 문구
│   └── music.py                # 일정 생성 중 음악 지시문·재시도 문구
├── planners/
│   └── travel.py               # 모델 요청 조율·구조화 출력 검증
├── services/
│   ├── matching.py             # 조건 필터·점수 계산·정렬·추천
│   ├── music.py                # 계절 필터·YouTube 후보·E5 정렬
│   └── travel/
│       ├── pipeline.py         # 장소 → 숙소 → 경로 → 음악 생성 흐름
│       ├── scheduling.py       # 일정 시간 배정·검증·경로 반영
│       ├── group_policy.py     # 인원별 일정 정책
│       ├── transport.py        # 날짜별 이동수단
│       ├── restaurant_hours.py # 영업시간 적용·검증
│       ├── streaming.py        # SSE 전송·제한 시간·연결 종료
│       └── backend_contract.py # 백엔드 이벤트·저장 결과 변환
├── clients/
│   ├── openai.py               # 모델 API 통신·응답 읽기
│   ├── kakao_places.py         # 장소 검색·후보 수집
│   ├── kakao_routes.py         # 이동 경로 조회·경로 응답 변환
│   └── youtube.py              # 곡 검색·공개 영상 검증
├── api/
│   ├── routes/                 # travel·music·matching·jobs·system HTTP 진입점
│   ├── examples/               # travel·music·matching Swagger 예시
│   ├── deps.py                 # 요청 변환·사전 검사·공통 헤더
│   ├── job_dispatch.py         # 큐 호출 변환
│   ├── responses.py            # 공통 OpenAPI 응답
│   ├── openapi.py              # Swagger 명세 설정
│   └── swagger_ui.py           # 문서 화면
├── core/                       # 설정·인증·오류·로그·앱 시작/종료
├── embeddings/
│   └── e5.py                   # 음악 추천 ONNX 모델 로드·추론
├── jobs/                       # 큐 접수·Redis 상태·Celery 워커·재시도
└── data/
    ├── travel/                 # 확인한 식당 영업시간 JSON
    └── matching/               # 개발 DB SQL·기준값·목데이터

scripts/
├── matching/                   # 개발용 DB 준비·합성 데이터·평가·연동 검증
└── models/                     # 모델 다운로드·체크섬 검증
tests/                          # core·travel·music·matching·jobs별 테스트
docs/                           # 구조 안내·기능별 연동 문서
artifacts/                      # 과거 평가·검증 결과
model/                          # 로컬 모델 자산, Git 제외
```

## 파일을 나누는 기준

각 기능의 스키마는 한 파일에서 봅니다. 예를 들어 매칭 조건·후보·요청·응답은 모두 `schemas/matching.py`에 둡니다. 요청과 응답을 각각 별도 폴더나 파일로 나누지 않습니다. 여러 기능이 쓰는 기반 모델은 `schemas/common.py`, Swagger 예시는 `api/examples/`에 둡니다.

프롬프트를 수정할 때는 `prompts/`, 요청 메시지와 JSON Schema를 조합하는 방법을 수정할 때는 `planners/`, 외부 API 통신을 수정할 때는 `clients/`를 봅니다. 장소 배치·매칭 점수와 같은 기능 규칙은 `services/`에 둡니다. 여행 처리는 시간 배정·SSE 등 역할이 커서 하위 폴더로 나누고, 짧은 음악·매칭 처리는 기능별 한 파일로 유지합니다.

개발 DB 도구는 `scripts/matching/`에서만 사용합니다. 운영 AI는 매칭 DB를 직접 조회하지 않으며 백엔드가 전달한 조건과 후보를 계산합니다.

## 수정할 파일 고르기

| 변경하려는 내용 | 먼저 볼 파일 |
| --- | --- |
| API URL·HTTP 응답 | `ai_service/api/routes/`의 해당 기능 파일 |
| 여행·음악·매칭 요청과 응답 | `ai_service/schemas/`의 해당 기능 파일 |
| 일정 생성 프롬프트 | `ai_service/prompts/travel.py` |
| 일정 생성 중 음악 프롬프트 | `ai_service/prompts/music.py` |
| 모델 요청 구성·출력 검증 | `ai_service/planners/travel.py` |
| 일정 시간·장소·숙소 배치 | `ai_service/services/travel/pipeline.py`, `scheduling.py` |
| SSE 이벤트 이름·백엔드 결과 | `ai_service/services/travel/backend_contract.py` |
| 음악 후보·계절·순위 | `ai_service/services/music.py` |
| E5 모델 로드·풀링·정규화 | `ai_service/embeddings/e5.py` |
| 매칭 점수·필터·정렬 | `ai_service/services/matching.py` |
| 개발용 합성 데이터·기준값 | `scripts/matching/dataset.py`, `catalog.py` |
| 개발 DB 오프라인 조회 | `scripts/matching/evaluation_data.py` |
| 큐 재시도·중복 처리 | `ai_service/jobs/worker.py`, `store.py`, `service.py` |
| 인증·오류 응답 | `ai_service/core/auth.py`, `handlers.py` |

## 호출 흐름

- 일정 JSON/SSE: `api/routes/travel.py` → 요청 변환·설정 검사 → `services/travel/pipeline.py` → 플래너·외부 클라이언트 → JSON 또는 백엔드 SSE 변환.
- 별도 음악 추천: `api/routes/music.py` → `services/music.py` → YouTube 검색 → E5 추론 → 영상 검증 → JSON.
- 동기 매칭: `api/routes/matching.py` → `schemas/matching.py` 검증 → `services/matching.py` → `{candidates, count}`.
- 큐 일정 생성: `api/routes/jobs.py` → `jobs/service.py` → Redis·Celery → `jobs/worker.py` → 일정 생성 → 결과 저장·상태 조회.

별도 음악 API는 E5, 일정 생성 중 음악 단계는 OpenAI를 사용합니다. 매칭은 테마·속도 계산식으로 추천하며 모델을 사용하지 않습니다.

## 실행과 검증

앱 진입점 `ai_service.main:app`, 워커 진입점 `ai_service.jobs.worker:celery_app`을 사용합니다. 프로젝트 루트에서 실행합니다.

```bash
./.venv/bin/python -m unittest discover -s tests -t .
```

외부 API는 테스트 대역으로 대체합니다. 로컬 모델이 있으면 실제 E5 검사도 실행하고, Redis 통합 검사는 필요한 실행 환경이 없으면 건너뜁니다.
