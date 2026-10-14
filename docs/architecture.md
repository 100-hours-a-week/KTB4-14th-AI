# 프로젝트 구조와 코드 찾는 방법

일정 생성, 음악 추천, 동행자 매칭, 큐 작업을 기능별로 구분합니다. HTTP 요청을 받는 코드는 `api/routes/`, 실제 처리와 데이터 계약은 각 기능 폴더에 둡니다.

```text
ai_service/
├── main.py                 # 앱 조립·라우터 등록, 실행 진입점
├── api/
│   ├── routes/
│   │   ├── system.py       # /health, /docs
│   │   ├── travel.py       # 일정 생성 JSON·SSE
│   │   ├── music.py        # 별도 E5 음악 추천
│   │   ├── matching.py     # 백엔드 후보 동기 추천
│   │   └── jobs.py         # 일정 큐 접수·상태 조회
│   ├── deps.py             # 일정 요청 변환·사전 검사·공통 헤더
│   ├── job_dispatch.py     # HTTP 요청에서 큐 서비스를 호출하는 변환 계층
│   ├── responses.py        # 공통 OpenAPI 응답 정의
│   ├── examples.py         # 일정·음악 API 요청·응답 예시
│   ├── openapi.py          # 실제 응답 계약에 맞춘 Swagger 설정
│   └── swagger_ui.py       # 문서 화면
├── core/
│   ├── config.py           # 환경 설정
│   ├── auth.py             # 서비스 Bearer 인증
│   ├── schemas.py          # 공통 모델 기반·지역·기간·오류 응답
│   ├── errors.py           # 서비스 예외
│   ├── handlers.py         # JSON·큐 예외 응답
│   ├── middleware.py       # 요청 ID
│   ├── diagnostics.py      # 구조화 로그
│   └── lifespan.py         # 앱 시작·종료, 클라이언트·서비스 생성
├── embeddings/
│   └── e5.py               # 음악·매칭에서 공유하는 ONNX 추론
├── travel/
│   ├── schemas.py          # 일정 요청·응답·장소·경로·모델 출력
│   ├── pipeline.py         # 장소 → 숙소 → 경로 → 음악
│   ├── features.py         # 시간 배정·검증·경로 반영
│   ├── model.py            # 일정 생성용 OpenAI 호출
│   ├── places.py           # 카카오 장소 조회
│   ├── routing.py          # 경로 조회·공개 경로 응답 변환
│   ├── transport.py        # 날짜별 이동수단
│   ├── group_policy.py     # 인원별 일정 정책
│   ├── restaurant_hours.py # 확인된 영업시간 적용·검증
│   ├── streaming.py        # SSE 전송·제한 시간·종료 처리
│   ├── backend_contract.py # 백엔드 이벤트·최종 저장 결과 변환
│   └── data/               # 확인된 식당 영업시간
├── music/
│   ├── schemas.py          # 음악 요청·응답·추천곡
│   ├── e5.py               # 후보 필터링·계절 반영·E5 정렬
│   └── youtube.py          # YouTube 검색·영상 검증
├── matching/
│   ├── schemas.py          # 프로필·조건·후보·순위
│   ├── service.py          # 조건 필터링·문장 변환·추천 순위
│   ├── backend.py          # 백엔드 DTO·match_rate 응답 변환
│   ├── catalog.py          # 백엔드 기준값 로드
│   ├── dev.py              # 합성 개발 데이터 생성
│   └── data/               # 개발 DB SQL·기준값·목데이터
└── jobs/
    ├── schemas.py          # 접수·상태·오류 계약
    ├── service.py          # 작업 접수·발행·결과 조회
    ├── store.py            # Redis 상태·중복 방지·lease·outbox
    ├── worker.py           # Celery 실행·재시도·진행 상태
    └── errors.py           # 큐 API 오류 계약

tests/                      # core·travel·music·matching·jobs별 테스트
scripts/matching/           # DB 준비·평가·백엔드 호출 검증
scripts/models/             # 모델 다운로드·체크섬 검증
docs/                       # 구조 안내·기능별 연동 문서
artifacts/                  # 평가·검증 결과
model/                      # 로컬 모델 자산, Git 제외
```

## 수정할 파일 고르기

| 변경하려는 내용 | 먼저 볼 파일 |
| --- | --- |
| API URL·HTTP 응답 | `ai_service/api/routes/`의 해당 기능 파일 |
| 일정 Request Body·최종 JSON | `ai_service/travel/schemas.py` |
| 일정 시간·장소·숙소 배치 | `ai_service/travel/pipeline.py`, `features.py` |
| SSE 이벤트 이름·백엔드 결과 | `ai_service/travel/backend_contract.py` |
| 음악 후보·계절·순위 | `ai_service/music/e5.py` |
| E5 모델 로드·풀링·정규화 | `ai_service/embeddings/e5.py` |
| 매칭 문장·필터·점수 | `ai_service/matching/service.py` |
| 백엔드 매칭 DTO | `ai_service/matching/backend.py` |
| 개발 DB 오프라인 조회 | `scripts/matching/evaluation_data.py` |
| 큐 재시도·중복 처리 | `ai_service/jobs/worker.py`, `store.py`, `service.py` |
| 인증·오류 응답 | `ai_service/core/auth.py`, `handlers.py` |

## 호출 흐름

- 일정 JSON/SSE: `api/routes/travel.py` → 요청 변환·설정 검사 → `travel/pipeline.py` → 기존 JSON 또는 백엔드 SSE 변환.
- 별도 음악 추천: `api/routes/music.py` → `music/e5.py` → YouTube 후보 검색 → 공통 E5 추론 → 영상 검증 → 최종 JSON.
- 백엔드 동기 매칭: `api/routes/matching.py` → `matching/backend.py` → `matching/service.py` → 공통 E5 추론 → `{candidates, count}`.
- 큐 작업: `api/routes/jobs.py` → `jobs/service.py` → Redis·Celery → `jobs/worker.py` → 일정 생성 서비스 → 결과 저장·상태 조회.

음악과 매칭 서비스는 `embeddings/e5.py`를 함께 사용합니다. 일정 생성의 음악 단계는 기존 OpenAI 경로를 유지하며 별도 음악 API의 E5 경로와 구분합니다.

## 구조 변경 검증

앱 실행은 `ai_service.main:app`, 워커 실행은 `ai_service.jobs.worker:celery_app`을 유지합니다. OpenAPI에서 매칭은 백엔드 DTO의 동기 200 계약만 제공하는지 확인합니다.

```bash
./.venv/bin/python -m unittest discover -s tests -t .
```

외부 API는 테스트 대역으로 대체합니다. 로컬 모델이 있으면 실제 E5 검사도 실행하고, Redis 통합 검사는 필요한 실행 환경이 없으면 건너뜁니다.
