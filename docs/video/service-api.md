# 영상 서비스 API와 AI DB

이 문서는 영상용 SQS 없이 작업을 접수하는 초기 구현을 설명합니다.
기존 CLI 영상 생성 기능에 API, PostgreSQL 작업 장부, 별도 실행기를 붙였습니다.
아직 운영 배포·프론트/백엔드 실연동·실제 저장소의 서명 URL 브라우저 재생까지 검증한 상태는 아닙니다.

## 각 구성 요소의 역할

- FastAPI: 사진과 생성 요청을 받고 작업 번호·진행 단계·재생 주소를 돌려주는 창구입니다.
- PostgreSQL: 업로드 사진의 경로와 소유자, 영상 작업의 상태·결과 경로를 기억합니다.
- 작업 실행기: 대기 작업을 하나씩 가져와 기존 AI 영상 생성 파이프라인을 실행합니다.
- RunPod GPU: 실제 영상 생성 계산을 합니다. `VIDEO_GPU_AUTOSTART=1`이면 실행기가 대기 작업이 있을 때 GPU Pod를 켜고, 대기열이 `VIDEO_GPU_IDLE_STOP_SECONDS`(기본 600초) 동안 비면 끕니다. GPU Pod를 새로 만들거나 삭제하지는 않습니다.
- RunPod Network Volume: 실제 사진·MP4·썸네일을 저장합니다. DB 자체는 이 볼륨에 설치하지 않습니다.

API와 실행기는 같은 CPU 서버의 별도 프로세스로 실행합니다. GPU를 꺼도 API와 DB는 계속 동작합니다.
compose 파일은 로컬 개발용 DB입니다.

### DB 데이터를 지키는 방법

RunPod Network Volume(MooseFS)은 postgres 사용자에게 모든 폴더를 0777로 보여 주므로, PostgreSQL이 그 위에서는 시작을 거부합니다.
그래서 DB 본체는 CPU Pod의 자체 디스크(`/var/lib/postgresql/data`)에서 실행하고, Network Volume(`/data/pg`)에는 복구용 기록을 둡니다.

- 변경 기록(WAL): 최대 60초마다 `/data/pg/wal`에 gzip으로 복사합니다. 갑작스러운 종료 시 잃을 수 있는 범위는 최대 약 1분입니다.
- 기준본(base backup): 백업 프로세스가 시작될 때와 이후 매일 `/data/pg/base`에 저장하고, 최근 2개만 남깁니다. 더 오래된 WAL은 자동 정리합니다.
- 자동 복구: Pod를 껐다 켜서 자체 디스크가 비면, 시작 스크립트가 최신 기준본을 풀고 WAL을 재생해 마지막 상태로 되돌립니다.
- 별도로 매일 `pg_dump` 파일을 미디어 볼륨의 `service-backups/video-db/`에 올리고 검증합니다.

## AI DB에 저장되는 내용

`video_uploads` 표는 업로드한 사진 한 장마다 사용자 번호와 저장소 안의 파일 경로(storage_key)를 기록합니다.
`video_jobs` 표는 영상 작업마다 다음을 기록합니다. 사진·MP4 파일 자체는 저장하지 않고 위치만 기록합니다.

| 열 | 뜻 |
| --- | --- |
| id | 작업 ID (백엔드가 만든 request_id) |
| owner_id | 사용자 ID |
| travel_plan_id | 여행 ID |
| photo_keys | 이 작업에 쓰는 사진들의 저장 경로 |
| status / stage | 진행 상태 / 세부 단계 |
| created_at / completed_at | 생성 요청 시간 / 완료(또는 실패) 시간 |
| error_code / error_message | 실패 원인 |
| video_key | 완성 MP4의 저장 경로 (재생 URL이 아님) |
| attempts | 실행기가 이 작업을 가져간 횟수 |

표와 열은 API·실행기가 시작할 때 자동으로 만들어지며, 이미 있는 데이터는 지우지 않습니다.

## 실행

`uv sync --extra video`로 영상 의존성과 PostgreSQL 드라이버를 설치합니다.
환경변수는 API와 실행기 양쪽 프로세스에 전달해야 합니다. 새 VIDEO_* 설정은 프로세스 환경변수를 읽습니다.
기존 RunPod/OpenAI/영상 설정은 기존 .env 설정을 사용합니다.

```sh
export VIDEO_DB_PASSWORD='로컬에서 정한 비밀번호'
docker compose -f compose.video.yml up -d --wait
export VIDEO_DATABASE_URL='postgresql://audigo:비밀번호@127.0.0.1:5433/audigo_video'
uv run python -m ai_service.services.video.jobs
uv run uvicorn ai_service.main:app --host 127.0.0.1 --port 8000
```

별도 터미널에서 같은 환경변수를 설정하고 실행합니다.

```sh
uv run python -m scripts.video.worker
```

VIDEO_DATABASE_URL이 설정되어야 영상 API가 등록됩니다. DB 초기화 명령은 기존 표를 삭제하지 않습니다.
실행기를 켜면 대기 요청이 실제 GPU와 OpenAI를 사용하므로 요금이 발생할 수 있습니다.
GPU·ComfyUI·모델·FFmpeg·음악 관련 기존 설정도 준비되어 있어야 합니다.

## 백엔드와의 약속

모든 아래 요청은 기존 `Authorization: Bearer <AUDIGO_API_TOKEN>` 및 `X-Owner-ID: <양의 사용자 번호>`가 필요합니다.
이 API는 신뢰하는 백엔드 전용입니다. 브라우저에 서비스 토큰을 넣으면 안 됩니다.
백엔드가 로그인 사용자와 여행 소유권을 확인한 후 사용자 번호를 전달해야 합니다.
AI 서버는 전달받은 사용자 번호로 작업·업로드 소유자를 검사합니다. 여행 소유권 자체를 조회하지는 않습니다.

### 사진 업로드

`POST /api/ai/v1/uploads`의 본문에 사진 한 장의 원본 바이트를 보냅니다. multipart가 아닙니다.
JPEG·PNG·WebP, 20 MiB 및 4천만 픽셀 이하입니다. 201 응답은 `{"upload_id":"UUID"}`입니다.
JPEG에는 휴대폰이 저장하는 MPO 형식(확장자 .JPG)도 포함됩니다.

여러 장은 `POST /api/ai/v1/uploads/batch`에 multipart `files` 필드로 한 번에 최대 30장까지 보냅니다.
사진마다 따로 저장하며, 모두 성공하면 201, 일부만 성공하면 207을 돌려줍니다.
응답의 `upload_ids`는 저장에 성공한 사진의 ID 목록이고, `items`에는 파일별 결과(`upload_id` 또는 `error`)가 들어 있습니다. 실패한 사진만 다시 올리면 됩니다.
Swagger(`/docs`)에서도 이 주소로 여러 파일을 선택해 시험할 수 있습니다.

### 작업 생성

`POST /api/ai/v1/video-jobs`

```json
{
  "request_id": "f92b8c91-badb-4a1e-bd1f-29e949236bc9",
  "travel_plan_id": 101,
  "upload_ids": ["사진1의 UUID", "사진2의 UUID"],
  "duration_seconds": 30,
  "memory_context": "강릉 바다 여행",
  "memory_style": "WARM",
  "music": "AUTO"
}
```

사진 2~120장, 목표 길이 5~30초를 받습니다. 실제 품질 검사 실패 시 작업은 실패할 수 있습니다.
request_id는 백엔드가 생성한 UUID입니다. 동일 번호·동일 요청 재전송은 같은 작업을 반환하고, 다른 내용 재사용은 409입니다.
202 응답은 job_id, status, status_url입니다. 요청이 접수됐다는 뜻이며 생성 완료를 뜻하지 않습니다.
음악은 AUTO/WARM/BRIGHT/CALM/NONE을 지원합니다. 사용자 음원 파일 업로드는 이 API에 아직 없습니다.

### 상태 조회

`GET /api/ai/v1/video-jobs/{job_id}`를 예를 들어 3~5초 간격으로 조회합니다.
status는 QUEUED → RUNNING → COMPLETED 또는 FAILED입니다.
stage는 QUEUED(대기 중) → ANALYZING_PHOTOS(사진 분석 중) → GENERATING_VIDEO(AI 영상 생성 중) → EDITING_VIDEO(영상 편집 중) → SAVING(저장 중) → COMPLETED(완료)이며, 실패 시 FAILED입니다. 응답의 stage_label에 한국어 이름이 들어 있고, 실패하면 error_code와 error_message에 원인이 남습니다. created_at·completed_at·travel_plan_id도 함께 반환합니다.
단계는 파이프라인이 남기는 진행 기록(report.json)을 실행기가 2초마다 읽어 갱신합니다. 세부 퍼센트는 제공하지 않습니다.
완료 시 video_url을 반환합니다. 이 주소는 서비스 API의 상대 경로이며, 프론트에는 백엔드가 사용할 수 있는 주소로 연결해야 합니다.

### 영상 제공

`GET /api/ai/v1/video-jobs/{job_id}/video`는 소유권 확인 후 5분 유효한 RunPod 서명 URL로 307 응답합니다. `?response_format=json`을 붙이면 이동하지 않고 `{"url": "...", "expires_in": 300}`을 돌려주므로 백엔드가 주소만 받아 프론트에 전달하기 쉽습니다.
백엔드는 이 Location을 얻어 사용자에게 전달할 수 있습니다. 프론트가 직접 AI API의 서비스 토큰을 사용하지 않습니다.
서명 URL을 아는 사람은 만료 전까지 파일을 읽을 수 있으므로 로그·공유 범위에 주의합니다.
DB에는 만료 URL이 아닌 영구 파일 경로를 저장하므로 만료 후 다시 발급할 수 있습니다.
실제 게이트웨이의 서명 요청, 브라우저 Range 탐색·CORS 동작은 배포 전 실환경 검증 대상입니다.

## 작업 중단과 운영에서 알아둘 내용

초기형은 DB의 잠금을 사용해 실행기 한 개만 동작하게 합니다. 여러 요청은 DB에 대기합니다.
실행기나 서버가 재시작되면 진행 중이던 작업은 다시 대기열(QUEUED)로 돌아가 처음부터 다시 생성합니다. 같은 작업이 3번 중단되면 WORKER_INTERRUPTED로 실패 처리합니다.
GPU에 이미 전달된 작업은 실행기 종료와 동시에 취소된다고 보장하지 않습니다. 재요청 전 GPU 대기열 확인이 필요합니다.
작업 시간 제한은 2시간입니다. 실패 세부 정보는 로컬 파이프라인 보고서를 확인합니다.
네트워크 단절로 DB 잠금 연결을 잃은 실행기의 GPU 작업까지 분산 제어하는 고가용성 구성은 아직 아닙니다.
초기 운영은 실행기 한 프로세스를 관리하고, 장애 시 GPU 작업을 확인한 뒤 재시작해야 합니다.

사진·영상 삭제 API, 보관 기한에 따른 정리, 사용자별 용량/요청 한도, 상세 진행률, 운영 배포 자동화는 후속 작업입니다.
완료 결과는 기존 저장 모듈이 파일을 원격에서 읽어 검증한 뒤 완료로 기록합니다.
출력 게시 ID와 서비스 작업 ID는 다를 수 있으며, AI DB의 publication 필드가 둘을 연결합니다.

## URL을 이해하는 방법

URL은 서버 기능을 찾아가는 주소입니다. URL 자체가 DB이거나 영상을 생성하는 것은 아닙니다.
상태 URL을 호출하면 서버가 DB를 읽고, 재생 URL을 호출하면 서버가 저장소 파일의 접근 주소를 발급합니다.
화면은 프론트가 만들고, 이 API를 호출해 받은 결과로 대기/생성/완료 화면을 바꿉니다.
