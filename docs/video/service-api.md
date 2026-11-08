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

## 서버리스 GPU (2026-11-05, 현재 운영 방식)

GPU는 RunPod 서버리스 **로드밸런싱 엔드포인트** `audigo-video-gpu-anyregion`(`4p4q8wwokz241c`, 템플릿 `po7qysviu7`)에서 돌아갑니다. 네트워크 볼륨을 붙이지 않아 모든 데이터센터의 GPU를 씁니다. 이전 몬트리올 엔드포인트 `p0p8deqt3w7fg9`는 GPU 품절(`throttled`)로 멈춘 적이 있어 최대 워커 0으로 꺼 두었고, 볼륨 연결도 끊었습니다. 작업 실행기가 GPU Pod를 직접 만들고 지우지 않습니다.

- 동시 제작 (GPU 풀): 같은 템플릿의 엔드포인트 10개(`audigo-video-gpu-anyregion`~`-10`, 각 최대 워커 1대, `idleTimeout` 60초: 일이 끝나고 1분 뒤 꺼짐)를 `COMFYUI_URL`과 `COMFYUI_EXTRA_URLS`에 모두 넣습니다. 작업 실행기(`serve_parallel`)가 대기 중인 영상마다 비어 있는 엔드포인트를 1~2개씩 따로 배정해 여러 영상을 동시에 만듭니다. 기본은 영상 1개에 1개(비용이 가장 적음, `VIDEO_MAX_GPUS_PER_JOB=2`로 바꾸면 한가할 때 2개로 장면을 두 줄로 나눠 2~3분 빠르지만 약 60% 비쌈)이고, 최대 10개를 동시에 만들며, 그 이상은 빈 GPU가 생길 때까지 대기열에서 기다립니다(`worker_state=ALL_GPUS_BUSY`). 엔드포인트마다 워커가 1대라서 한 작업의 업로드·생성·다운로드가 항상 같은 GPU로 가고, 다른 작업과 섞이지 않습니다. 최근에 끝난(아직 켜져 있을 수 있는) 엔드포인트를 먼저 배정합니다. RunPod 계정의 서버리스 동시 GPU 한도가 10대(`maxServerlessConcurrency`)라서 엔드포인트를 더 늘리려면 한도 증가가 먼저 필요합니다.
  - 엔드포인트 ID: `4p4q8wwokz241c`, `381eowoojkelqh`, `p6wa4nd5mvb3un`, `zof0wl7iev3pgo`, `wpajwmtxr3lvyu`, `lms4jttb5gs2mc`, `41ivch6hftpva2`, `jxdpx8etcfebs1`, `ng4ll6mx40f2er`, `0ij42c8szvt9eg`
  - 사진 분석과 최종 편집은 CPU 서버(4코어) 한 대에서 하므로, 동시에 많이 몰리면 그 단계가 느려질 수 있습니다.
- 켜고 끄기: 작업 실행기가 `https://4p4q8wwokz241c.api.runpod.ai`로 요청을 보내면 RunPod가 워커를 켭니다. 600초(`idleTimeout`) 동안 요청이 없으면 끕니다. 최대 워커 수는 1대입니다.
- GPU: `AMPERE_80`(A100 80GB) → `BLACKWELL_96`(RTX PRO 6000 96GB) → `ADA_80_PRO`(H100 80GB) 순서로, 비어 있는 GPU 1대를 씁니다. 한 종류가 품절이어도 다음 종류로 켜집니다. 켜져 있는 초만큼만 청구됩니다(A100 약 $2.7/시간, H100은 더 비쌈).
- 품절 표시: 세 종류가 모든 데이터센터에서 없으면 작업 실행기 상태가 `GPU_SOLD_OUT`("GPU 품절")이 되고, 빈 GPU가 생기면 바로 시작합니다.
- 진행 표시: 작업 중에는 `progress_detail`에 지금 하는 일(OpenCV 사진 검사, OpenAI 장면 구성, 음악 작곡, 장면 i/N 생성, ffmpeg 합치기)이 들어갑니다.
- 시작 시간: 이미지가 캐시돼 있으면 컨테이너 시작 후 모델 6개(53GB)를 Hugging Face에서 워커 디스크로 받는 데 약 4~5분 걸립니다. 그동안 `/ping`은 204(준비 중)를 돌려주고, 끝나면 200을 돌려줍니다.
- 인증: RunPod 엣지는 `Authorization: Bearer <RUNPOD_API_KEY>`를 검사합니다. 그래서 게이트 토큰은 `X-Audigo-Token`으로 보냅니다(`VideoSettings.gpu_headers()`). `/ping`만 토큰 없이 응답합니다.
- 볼륨 없음: 시작 명령에 게이트와 모델 다운로더가 들어 있고, ComfyUI는 이미지에 포함돼 있습니다. 볼륨을 붙이면 `gpu_boot.sh`가 `/runpod-volume`을 `/workspace`로 연결합니다.
- 제한: 요청 하나는 5.5분 안에 끝나야 하고, 요청·응답은 각각 30MB 이하여야 합니다. ComfyUI 호출(업로드, 작업 등록, 상태 조회, 결과 받기)은 모두 이 안에 들어옵니다.
- CPU 서버 설정: `/data/config/overrides.env`에 `VIDEO_ENDPOINT_MODE=serverless`, `COMFYUI_URL=https://4p4q8wwokz241c.api.runpod.ai`, `VIDEO_GPU_AUTOSTART=0`이 있습니다. 이 파일을 바꾼 뒤 `supervisorctl -c /app/deploy/runpod-video/supervisord.conf restart api worker`를 실행하면 Pod 재시작 없이 적용됩니다.
- 시작 스크립트를 바꾼 뒤에는 템플릿의 `dockerStartCmd`를 `gpu_power.START_COMMAND`로 다시 올려야 합니다.
- 로그 보기: `runpodctl serverless logs 4p4q8wwokz241c`

## GPU Pod 구성 (2026-11-04, 이전 방식)

- 모델 보관함: 없습니다. 서버리스 GPU가 켜질 때마다 `deploy/runpod-gpu/download_models.py`로 Hugging Face에서 모델 6개(53GB)를 받습니다(3~4분). 예전 몬트리올 볼륨 `audigo-gpu-models-mtl`(`i1rifh8j8j`)은 2026-11-09에 지웠습니다.
- 필요할 때 GPU 만들기: `VIDEO_GPU_AUTOSTART=1`이면 작업 실행기가 이 볼륨에 선택한 GPU Pod(이름 `audigo-video-gpu-…`)를 준비합니다. 기본 선택지는 RTX PRO 6000 96GB와 A100 PCIe 80GB입니다. `AUTO`는 허용 목록 안에서 사용 가능한 GPU 한 대를 사용합니다. GPU 모델별로 준비된 Pod를 재사용하고, 대기열이 10분 동안 비면 관리 중인 Pod를 정리합니다.
- 모델 확인: GPU의 ComfyUI에 모델 6개가 모두 보일 때만 작업을 가져갑니다. 모델이 없는 GPU에는 작업을 보내지 않습니다.
- 비밀번호 관문: ComfyUI는 자체 인증이 없으므로 GPU Pod는 `deploy/runpod-gpu/gate.py`(볼륨의 `/workspace/audigo/gate.py`)만 8189 포트로 공개하고 ComfyUI의 8188은 열지 않습니다. `COMFYUI_API_TOKEN`이 맞는 요청만 통과합니다. 실서버에서 비밀번호 없는 요청은 모두 401, 8188 직접 접속은 불가함을 확인했습니다.
- 사진·영상 사본 삭제: 작업이 끝나면(성공·실패 모두) GPU의 업로드 사진·생성 파일과 CPU 작업 폴더의 사진·영상·음악 사본을 지웁니다. 원인 확인용 JSON 보고서만 남깁니다. 원본 사진과 완성 영상은 미디어 볼륨에만 있습니다.
- RunPod는 모든 Pod에 `RUNPOD_POD_ID`(그 Pod 자신의 ID)와 `RUNPOD_API_KEY`(권한이 제한된 키)를 자동으로 넣습니다. 그래서 특정 GPU 지정은 `VIDEO_GPU_POD_ID`를 쓰고, 계정 API 키는 `runtime.env`에서 덮어씁니다.

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
JPEG·PNG·WebP·HEIC(아이폰 기본 형식), 20 MiB 및 4천만 픽셀 이하입니다. 201 응답은 `{"upload_id":"UUID"}`입니다.
JPEG에는 휴대폰이 저장하는 MPO 형식(확장자 .JPG)도 포함됩니다. HEIC는 방향을 바로잡은 JPEG로 바꿔 저장합니다.
이 API만으로 영상이 만들어지지는 않습니다. 영상 생성 요청에는 upload_id가 2장 이상 필요하고, 1장 업로드는 1장씩 올리거나 실패한 사진을 다시 올릴 때 씁니다.

영상 API의 오류 응답은 `{"message":"<오류 코드>","data":{"error_message":"<한글 설명>"}}`입니다.
코드: `invalid_image`, `unsupported_image_format`, `image_too_many_pixels`, `image_too_large`, `upload_timeout`,
`daily_upload_limit_exceeded`, `invalid_photo_count`, `duplicate_upload_ids`, `upload_not_found`, `request_id_conflict`,
`too_many_pending_jobs`, `gpu_not_selectable`, `invalid_offset`, `job_not_found`, `job_already_finished`, `video_not_ready`,
`playback_signature_invalid`, `range_not_satisfiable`, `video_not_found`. 여러 장 업로드의 실패 항목에도 같은 `code`가 들어갑니다.

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
음악은 AUTO/WARM/BRIGHT/CALM을 지원합니다. 자동 작곡이 두 차례 품질 검사를 통과하지 못하면 분위기에 맞는 기본 반주를 사용합니다. 실행기는 배경음악이 들어간 영상만 완료 처리합니다. 사용자 음원 파일 업로드는 이 API에 아직 없습니다.

### 상태 조회

`GET /api/ai/v1/video-jobs/{job_id}`를 예를 들어 3~5초 간격으로 조회합니다.
status는 QUEUED → RUNNING → COMPLETED 또는 FAILED입니다.
stage는 QUEUED(대기 중) → ANALYZING_PHOTOS(사진 분석 중) → GENERATING_VIDEO(AI 영상 생성 중) → EDITING_VIDEO(영상 편집 중) → SAVING(저장 중) → COMPLETED(완료)이며, 실패 시 FAILED입니다. 응답의 stage_label에 한국어 이름이 들어 있고, 실패하면 error_code와 error_message에 원인이 남습니다. created_at·completed_at·travel_plan_id도 함께 반환합니다.
단계는 파이프라인이 남기는 진행 기록(report.json)을 실행기가 2초마다 읽어 갱신합니다. 세부 퍼센트는 제공하지 않습니다.
완료 시 video_url을 반환합니다. 이 주소는 서비스 API의 상대 경로이며, 프론트에는 백엔드가 사용할 수 있는 주소로 연결해야 합니다.

### 작업 취소

`POST /api/ai/v1/video-jobs/{job_id}/cancel`로 취소합니다. 다른 사용자의 작업은 404입니다.

- 대기 중이면 바로 취소되고 200 `{"status": "CANCELLED"}`를 돌려줍니다. GPU 비용이 들지 않습니다.
- 만드는 중이면 202 `{"status": "CANCELLING"}`을 돌려주고, 작업 실행기가 2초 안에 생성 프로세스를 멈추고 GPU의 진행 중 작업과 대기열을 비운 뒤 `CANCELLED`로 바꿉니다. 그동안 상태는 "취소하는 중"으로 보입니다. 실서버에서 4초 만에 멈추는 것을 확인했습니다.
- 이미 끝난 작업(완료·실패·취소)은 409입니다.
- 취소한 작업의 사진·영상 사본도 GPU와 CPU에서 지웁니다. 서버가 재시작되어도 취소 요청이 있던 작업은 다시 대기열에 넣지 않습니다.

### 영상 제공

`GET /api/ai/v1/video-jobs/{job_id}/video`는 소유권을 확인한 뒤 서비스가 서명한 재생 주소로 307 이동합니다. `?response_format=json`을 붙이면 `{"url": "...", "expires_in": null}`을 돌려줍니다. 재생 주소는 만료되지 않으며 MP4를 Range 요청(구간 탐색)과 함께 전달합니다. RunPod S3 게이트웨이는 presigned URL을 지원하지 않아 서비스가 직접 전달합니다.
백엔드는 이 Location을 얻어 사용자에게 전달할 수 있습니다. 프론트가 직접 AI API의 서비스 토큰을 사용하지 않습니다.
재생 주소를 아는 사람은 누구나 그 영상을 볼 수 있으므로 로그·공유 범위에 주의합니다. 주소의 서명은 작업 번호만으로는 만들 수 없습니다.
지금까지 발급한 모든 재생 주소를 막으려면 서버의 `VIDEO_PLAYBACK_SECRET`(없으면 `AUDIGO_API_TOKEN`) 값을 바꿉니다.
DB에는 재생 주소가 아닌 파일 경로를 저장합니다. 2026-11-04 RunPod 실서버에서 원본과 같은 바이트 전달, Range 탐색, 브라우저 재생을 확인했습니다.

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

### GPU 비용 (2026-11-09 실측 단가)
- 서버리스 GPU 1대 약 $5/시간 (GPU 9대 동시 가동 시 계정 전체 $44.9/시간 확인).
- GPU 비용은 켜지는 시간(모델 내려받기 3~4분) + 생성 시간 + 끝난 뒤 대기(60초)에 모두 붙습니다.
- 동시에 몇 개를 만들든 영상 1개 값은 같고, 한꺼번에 몰리면 시간당 비용만 커집니다(10대면 약 $50/시간).
