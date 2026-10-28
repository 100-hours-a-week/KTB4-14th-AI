# AUDIGO Wan2.2 FLF2V 파이프라인

## 기존 코드 분석과 연동 범위

현재 서비스는 FastAPI(`main.py`), 외부 통신(`clients/`), 공통 Pydantic 스키마(`schemas/common.py`), 기능별 처리(`services/`), 일정 전용 SQS(`sqs/`)로 구성되어 있다. 일정·음악·매칭 API는 존재하지만 영상 생성 API, 영상 DTO, 체크리스트 독립 API는 이 레포에서 찾지 못했다. 기존 여행 서비스의 일정 생성·후속 기능은 변경하지 않았다.

공통 `StrictModel`, `.env` 로딩 방식, 안전한 `core.diagnostics.record` 로그를 재사용한다. ComfyUI/RunPod HTTP 클라이언트와 영상 전용 설정·입력 검증·전처리·계획·인코딩·기록을 추가했다. 기존 HTTP 경로나 SQS v1 메시지에 영상 필드를 추가하지 않았다. 백엔드 영상 DTO가 합의되면 해당 어댑터에서 아래 Python 서비스 함수를 호출한다. 이 함수는 서버에 이미 준비된 로컬 사진 파일을 받는다. 사용자 브라우저 업로드, S3 접근·서명 URL, DB 작업 상태, 프론트 메시지, 클라우드 배포는 별도 계약/담당 범위다.

```python
import httpx
from ai_service.schemas.video import VideoRequest
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.pipeline import VideoPipeline

# 애플리케이션 lifespan에서 HTTP client와 pipeline/resolver를 재사용하면
# RunPod 조회 TTL/lock도 여러 작업 사이에 공유된다. 기존 main.py에 등록하지 않았다.
async def make_video(local_photos, target_seconds):
    settings = VideoSettings.from_env()
    async with httpx.AsyncClient(follow_redirects=False) as http:
        pipeline = VideoPipeline(http, settings)
        return await pipeline.generate(VideoRequest(
            photos=local_photos, duration_seconds=target_seconds,
        ))
```

긴 작업을 HTTP request의 기존 300초 제한이나 FastAPI 임시 BackgroundTasks 안에 넣지 않는다. 현재 CLI는 한 작업을 순차 처리하는 검증 도구다. 프로세스가 종료되면 자동 재개하지 않으며, `report.json`은 업무용 작업 DB/분산 큐가 아니다. API 서버의 이벤트 루프에서는 OpenCV 처리를 스레드로 넘기고 FFmpeg는 비동기 자식 프로세스로 실행한다.

## 실행 흐름과 파일

| 단계 | 모듈 | 동작 |
| --- | --- | --- |
| 입력 | `schemas/video.py` | 사진 2~91장, 최종 길이 정수 10~180초; smoke는 사진 2장·1~5초 |
| 사진 | `services/video/images.py` | 파일당 25MB, JPEG/PNG/WebP, 32px 이상·5천만 화소 이하, 디코딩 검사, EXIF 1~8, OpenCV 여백 처리 |
| 계획 | `services/video/planning.py` | 입력 순서 유지, 사진 쌍당 최소 2초, 생성 클립당 최대 5초·81프레임 |
| 주소 | `clients/runpod.py` | 고정 URL 또는 실행 중 Pod 탐색, 실제 ComfyUI 준비 상태 검사 |
| 그래프 | `services/video/workflow.py` | 공식 기본 분기 기반 API JSON, 실행 서버의 `/object_info`로 노드·입력·모델 확인 |
| 생성 | `clients/comfyui.py` | 업로드 → prompt ID → history/queue → 결과 파일 스트리밍 |
| 후처리 | `services/video/media.py` | 클립 검증, concat, H.264/yuv420p/16fps/720p/faststart, ffprobe 검증 |
| 관측 | `services/video/pipeline.py` | 단계·prompt ID·클립 시간·최종 시간·실패 사유·비용 추정, 원자적 report 갱신 |
| 실행 | `scripts/video/run.py` | check, smoke, generate, summarize |

모델 및 작업 파일은 `/workspace` 아래에 둔다. 각 작업의 `VIDEO_WORKSPACE/<job_id>/`에는 전처리 PNG, 제출한 API JSON, 다운로드한 클립, concat 목록, 최종 `final.mp4`, `report.json`이 생긴다. 출력은 작업마다 새 디렉터리로 격리한다. 로그에는 원본 사진 경로·프롬프트·인증키·외부 오류 본문을 남기지 않는다. 로컬 제출 JSON에는 사용자 프롬프트가 포함되므로 사용자 파일과 함께 접근을 제한한다. 작업 디렉터리 삭제 및 보관 기간은 운영 담당자가 결정한다.

원본을 잘라내지 않는 여백 방식이므로 세로 사진에는 검은 여백이 들어간다. 사진 순서는 EXIF 날짜 등으로 재정렬하지 않고 호출자가 전달한 순서를 쓴다. 사진이 너무 많아 모든 인접 쌍에 최소 2초를 배정할 수 없으면 명시적으로 거절한다(10초: 최대 6장, 180초: 최대 91장).

검증을 위해 원본 전처리본과 중간 클립을 보존한다. 500편을 250GB 디스크 한 곳에 계속 누적하는 운영은 검증하지 않았다. 최종 파일만 편당 상한 500MB로 계산해도 250GB가 되므로, 대량 제작 전에는 결과 외부 보관과 검증 완료 중간 파일 정리 정책이 필요하다.

클립 수는 `max(사진 수 - 1, ceil(요청 초 / 5))`이다. 모든 인접 사진 쌍에 클립 수를 균등 배분한다. 각 쌍의 첫 클립은 `사진 A → 사진 B`; 추가 클립은 `B → B`를 두 조건으로 새로 추론하는 `motion_hold`다. 2장·10초는 A→B, B→B 두 번의 추론이다. 이는 긴 길이를 지원하기 위한 기본 정책이며, 사진이 적은 180초 결과의 장면 다양성·정체성 유지 품질은 보장하지 않는다. 추가 사진을 제공하는 쪽이 다양한 장면 구성을 얻기 좋다.

Wan의 시간축 제약에 맞게 각 클립은 `4k+1` 프레임을 생성한다. `CreateVideo.fps = 생성 프레임 수 / 배정 초`로 길이를 맞춘다(예: 81프레임 / 5초 = 16.2fps). FFmpeg에서 16fps로 통일하며 마지막 프레임 단위 오차만 정리한다. FFmpeg는 생성 클립을 연결·인코딩하며 사진 확대/축소·페이드·프레임 보간·클립 복제·정지 프레임 연장으로 움직임이나 길이를 대체하지 않는다.

## 공식 워크플로 및 API 근거

- [요청한 공식 FLF2V 워크플로](https://comfy.org/workflows/video_wan2_2_14B_flf2v-7016f027bcf1/)
- [핀한 공식 템플릿 원본](https://github.com/Comfy-Org/workflow_templates/blob/e7cd011d4ded3411c2f481200544f0be6fdc962e/templates/video_wan2_2_14B_flf2v.json)
- [ComfyUI 0.35.0 Wan 노드](https://github.com/Comfy-Org/ComfyUI/blob/v0.35.0/comfy_extras/nodes_wan.py)
- [0.35.0 영상 노드](https://github.com/Comfy-Org/ComfyUI/blob/v0.35.0/comfy_extras/nodes_video.py), [HTTP 서버](https://github.com/Comfy-Org/ComfyUI/blob/v0.35.0/server.py), [API 경로 문서](https://docs.comfy.org/development/comfyui-server/comms_routes)

`workflows/wan2_2_flf2v.ui.json`은 보관용 UI 원본이다. 서버에 보내는 것은 `wan2_2_flf2v.api.json`이며 기본 활성 분기만 포함한다. UI link를 보존하고, 0.35.0 소스의 입력 이름으로 위젯을 API 입력으로 변환했다. 출처 revision과 UI SHA-256은 `provenance.json`, 원본 라이선스는 `LICENSE`에 있다. UI JSON을 API JSON인 것처럼 `/prompt`로 전송하지 않는다.

| 노드 | ID / 입력 |
| --- | --- |
| 시작/끝 사진 | LoadImage `80`, `89`: `image` |
| FLF2V 조건 | `81`: `start_image=["80",0]`, `end_image=["89",0]`, 1280×720, length, batch_size=1 |
| 모델 | UNETLoader `76` 고노이즈, `77` 저노이즈, FP8 scaled I2V A14B |
| 텍스트/VAE | CLIPLoader `72` type=wan, VAELoader `79` Wan2.1 VAE |
| 프롬프트 | `90` positive, `78` negative |
| 샘플러 | `84` 0~10단계 → `87` 10~20단계, euler/simple, cfg=4, shift=8 |
| 출력 | CreateVideo `86` → SaveVideo `83` |

L40S용으로 텍스트 인코더 기본 device만 `cpu`로 둔다. FP8 가중치 파일을 쓰며 기본 20단계 분기를 유지한다. Lightning LoRA는 포함하지 않았다. `SaveVideo`의 0.35.0 DynamicCombo wire 형식은 `format="mp4"`, `format.codec="h264"`, `format.codec.encoding="auto"`다. 입력을 추정해서 서버에 보내지 않고 실제 `/object_info`와 맞지 않으면 제출 전에 중단한다. 다른 버전으로 변경하면 워크플로와 서버 스키마를 다시 검증해야 한다.

API 순서는 다음과 같다.

1. `GET /system_stats`, `GET /object_info`로 준비 상태·모델 파일명을 확인한다. live 모드는 CUDA 장치가 없으면 제출 전에 중단한다. 준비 상태는 모델 추론 성공을 뜻하지 않는다.
2. `POST /upload/image`: multipart `image`, `type=input`, `subfolder`, `overwrite=false`. 서버가 반환한 `name`, `subfolder`, `type`을 사용하며 원래 파일명을 가정하지 않는다.
3. `POST /prompt`: JSON `{prompt: API그래프, client_id: 작업ID}`. 반환한 `prompt_id`를 즉시 기록한다. HTTP 400 및 node_errors는 실패로 처리한다.
4. `GET /history/{prompt_id}` 및 `/queue`로 queued/running/completed 상태를 확인한다. 표준 history API는 샘플링 퍼센트를 제공하지 않으므로 임의의 퍼센트를 만들지 않는다.
5. status의 error/interrupted를 확인하고, 완료된 `outputs["83"]["images"]`에서 MP4 파일 정보를 받는다. 0.35.0의 SaveVideo/PreviewVideo는 비디오도 `images` 키로 반환한다.
6. `GET /view?filename=...&subfolder=...&type=output`으로 스트리밍 다운로드한다. 파일 크기 상한·부분 파일 삭제·ffprobe 검사 후 다음 클립으로 넘어간다.

## 주소 변경 대응

[RunPod List Pods](https://docs.runpod.io/api-reference/pods/GET/pods), [RunPod REST OpenAPI](https://rest.runpod.io/v1/openapi.json), [공식 HTTP 프록시 규칙](https://docs.runpod.io/pods/configuration/expose-ports)을 기준으로 구현했다.

`VIDEO_ENDPOINT_MODE=runpod`는 `GET https://rest.runpod.io/v1/pods`에 RunPod 계정 Bearer 키를 보내 실행 중 GPU Pod 목록을 받는다. `name` 접두사, `desiredStatus=RUNNING`, 노출 포트 `8188/http`, 선택적 Pod ID를 확인한다. REST Pod 객체에 임의의 `url` 필드가 있다고 가정하지 않는다. 공식 규칙인 `https://<pod-id>-<port>.proxy.runpod.net`으로 주소를 만들고 `/system_stats`를 검사한다. 여러 후보면 ID 순으로 준비된 첫 Pod를 고른다. 부하 분산기는 아니다.

주소는 기본 30초 TTL로 캐시하며, 캐시를 쓸 때도 ComfyUI 준비 상태를 검사한다. 캐시가 살아 있으면 RunPod 제어 API가 잠시 중단돼도 그 TTL 안에서는 작업을 시작할 수 있다. 캐시 만료 후 RunPod API 장애에는 명시적으로 실패한다. 무기한 오래된 주소를 계속 사용하지 않는다. 새 Pod가 아직 부팅 중이거나 재고가 없으면 `runpod_no_ready_pod`를 기록한다.

**한 작업의 모든 업로드·prompt·history·다운로드는 최초 선택한 Pod에 고정한다.** 실행 도중 주소를 다시 골라 다른 Pod에 기존 prompt ID를 조회하지 않는다. 실패하면 다음 새 작업에서 재탐색하도록 캐시를 무효화한다. 제출 응답 유실은 이미 GPU에 등록됐을 수 있어 `comfyui_submission_uncertain`으로 처리하고 자동 재전송하지 않는다. timeout/프로세스 취소 후 GPU 작업이 계속 돌 수 있으면 `gpu_work_may_still_be_running`을 남긴다. 다른 사용자의 작업을 멈출 수 있는 글로벌 `/interrupt`는 호출하지 않는다.

장애 후에는 기록된 **같은 Pod와 prompt ID**로 history/queue 및 과금 상태를 확인한 뒤 재시도한다. 자동 복구·클립 재개·작업 중 Pod 이전은 구현하지 않았다. discovery 키는 RunPod API에만 보내며 ComfyUI에는 별도 gateway 키만 보낸다.

추후 운영용으로는 기존 AWS 환경을 활용한 **영상 전용 SQS 요청/결과 계약과 GPU pull worker**가 적합하다. GPU worker가 자기 Pod의 localhost ComfyUI를 호출하면 백엔드는 Pod 주소를 알 필요가 없다. 다만 별도 영상 메시지 스키마·원본 저장소·결과 저장소·멱등성·visibility lease·DLQ 계약이 먼저 필요하다. 현재 일정 전용 SQS에 임의 stage를 추가하거나 Kafka/Consul 인프라를 구축하지 않았다.

## 측정과 500편 검증 기준

모든 시각은 UTC ISO 문자열, 기간은 monotonic clock 초 단위다. `report.json`에 전체 소요, 단계별 elapsed, prompt별 처리 시간, 가능한 경우 history execution_start→execution_success 시간, 출력 길이·픽셀·코덱·용량·런타임 버전·GPU 메모리 정보를 기록한다. provider execution 시간은 큐/로딩을 포함할 수 있으며 GPU 커널 사용률 측정값이 아니다.

`estimated_pod_reserved_seconds`는 endpoint 준비 확인부터 최종 파일 완성/실패 기록까지의 관측 구간이다. `estimated_request_window_cost_usd = 관측 초 / 3600 × VIDEO_GPU_HOURLY_USD`. Pod가 켜져 있으면 CPU 후처리·다운로드 중에도 비용이 나므로 이 구간을 포함한다. 발견 이전 설치/부팅 시간, 대기, 작업 간 idle, 별도 저장비, 종료되지 않은 orphan 작업은 제외되므로 **실제 청구액이 아니다**. `gpu_billed_seconds`, `actual_cost_usd`는 측정하지 못했으므로 null이다. 실제 운영에서는 RunPod 세션 시작/종료 및 청구 내역과 대조해야 한다.

Mock는 `execution_mode=mock`, `status=mock_completed`로만 저장한다. GPU 관측 시간과 비용은 null이며, `provider_execution_seconds` 또한 Mock의 합성 응답이다. Mock 결과로 실측 GPU 원가나 500편 생산성을 계산하지 않는다. summary는 Mock/live와 smoke/final, 요청 길이를 각각 나누고 실패한 시도의 관측 비용도 합산한다. 취소 후 남은 GPU 비용은 여전히 추적 대상이다.

참고 단가 $1.11/h에서 총 $269는 약 **242.34 Pod 시간**, 500편 기준 영상당 **$0.538 / 약 29.08분**의 총 예약 시간 한도다. 여기에는 설치·대기·실패·재생성·보관 비용의 여유가 아직 없다. 2026-11-17 목표까지 가능한지는 현재 미검증이다. 먼저 실제 5초 smoke의 cold/warm 추론·메모리·시작/끝 조건 및 움직임 품질을 확인하고, 10/30/60/180초별로 시간·재시도율·채택률을 반복 측정한다. `summarize`가 중앙값/최대 소요와 실패 포함 관측 원가를 비교해도 생산 가능 여부를 자동으로 참이라고 표시하지 않는다.

GPU 검증 시 사람·랜드마크 보존, 중간 프레임 실제 움직임, 사진 끝 조건, B→B 구간 움직임, 클립 경계의 튐, 긴 결과의 장면 다양성을 직접 확인한다. ffprobe 통과는 영상 품질/실제 AI 움직임을 입증하지 않는다. 단일 L40S에서 720p·81프레임·FP8·20단계의 속도와 OOM 여부는 Pod 확보 후 검증해야 한다.
