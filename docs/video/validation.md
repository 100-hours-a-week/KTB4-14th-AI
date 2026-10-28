# 영상 파이프라인 검증 기록 — 2026-10-28 (KST)

실제 RunPod Pod/GPU는 확보되지 않아 **Wan2.2 추론을 실행하지 않았다.** 아래는 로컬 CPU의 합성 API 응답과 FFmpeg 검증 결과다. 실제 AI 생성 성공률·GPU 속도·품질·원가를 나타내지 않는다.

## 실행 결과

```bash
uv run --frozen --extra video --extra dev pytest tests/video -q
# 19 passed
uv run --frozen --extra video --extra dev pytest tests -q
# 274 passed, 1 skipped; 기존 Starlette/AnyIO deprecation warning 1건
uv lock --check
git diff --check
```

사진 2장·클립 1개 smoke, 사진 3장·10초 최종 영상, 사진 2장·11초 최종 영상을 테스트했다. 고정 25/30초가 아닌 10~180초 모든 정수 요청의 계획을 검사했다. 기존 API·SQS 관련 회귀 테스트도 통과했다. 선택 의존성 OpenCV/Pillow를 추가했으며 기존 잠금 패키지 버전은 바꾸지 않았다.

영상 선택 의존성이 없는 독립 환경에서도 기존 FastAPI `/health` 200을 확인했다. wheel 빌드 및 API/UI 워크플로 JSON·provenance·원본 라이선스 포함 여부도 확인했다.

별도의 CLI 실행으로 사진 3장을 넣어 다음 결과를 얻었다. Mock의 `/view`는 FFmpeg 합성 테스트 패턴을 반환하며 같은 설정의 테스트 패턴 데이터를 캐시한다. **실제 파이프라인의 각 클립은 별도 prompt ID로 제출되며 실제 생성 미디어를 재사용하는 구현은 없다.**

| 요청 길이 | Mock 제출 클립 수 | 실제 인코딩 결과 길이 | 파일 크기 (bytes) | 로컬 전체 소요 (초) | GPU 비용 |
| --- | --- | --- | --- | --- | --- |
| 10초 | 2 | 10.000초 | 3,288,426 | 0.879 | 미측정/null |
| 30초 | 6 | 30.000초 | 9,867,309 | 2.349 | 미측정/null |
| 180초 | 36 | 180.000초 | 59,222,046 | 11.869 | 미측정/null |

모두 ffprobe로 H.264, yuv420p, 1280×720, SAR 1:1, 16fps, MP4 컨테이너와 500,000,000 bytes 이하를 확인했다. 시간은 로컬 도구 경로의 관측값이며 회귀 테스트와 함께 실행했으므로 성능 비교용 벤치마크가 아니다. AI 확산 추론 비용을 포함하지 않는다.

원본 실행 기록은 Git에서 제외한 `artifacts/video/verification/jobs/<job_id>/report.json`, 집계는 `artifacts/video/verification/summary.json`에 있다. 가중치·입력 사진·클립·최종 파일도 Git에서 제외한다.

## 검사한 실패/경계 조건

- 손상·누락·과대 파일, 작은 이미지, JPEG/PNG/WebP, EXIF 1~8 회전/반전과 전체 사진 보존.
- 공식 UI 원본 SHA-256, 활성 기본 분기의 실제 링크, 두 사진 조건과 고/저노이즈 샘플러 연결 보존.
- 실제 `/object_info` 형식에 대한 노드/모델/입력/DynamicCombo 검증. 잘못된 모델/코덱은 제출 전에 실패.
- multipart 업로드 후 서버가 바꾼 이름 사용, `/prompt` wire payload, history의 SaveVideo `images`, `/view` 파라미터.
- prompt 거절, execution error, 완료 결과 누락, timeout, 제출 응답 유실, 다운로드 실패, 파일 용량 상한, 부분 파일 제거.
- 응답 유실/timeout 시 prompt 재전송 및 글로벌 interrupt 금지, 실패 이유/소요/미완료 GPU 작업 가능성 기록.
- RunPod REST 인증 분리, 이름·상태·HTTP 포트 필터, 준비 상태, 캐시, 교체 Pod 재탐색, API 장애, 선택적 Pod ID 제한.
- 한 작업의 여러 prompt가 최초 Pod를 유지하고 중간에 새 Pod가 생겨도 갈아타지 않음.
- Mock 기록의 GPU 예약 시간/원가 null, Mock/live 집계 분리와 500편 가능 여부 `not_validated`.
- live 모드에서 CUDA 장치가 없으면 prompt를 제출하지 않음. 시스템 응답의 argv 등 비공개 정보가 기록에 포함되지 않음.

## 아직 필요한 GPU 검증

README의 모델 설치 → `check` → `smoke --photos ... ...` 순서로 진행한다. 실제 서버의 `/object_info` 통과, L40S 48GB에서 720p·81프레임 FP8 기본 분기의 메모리/시간, 실제 두 사진 조건과 중간 움직임, B→B 구간 움직임, 클립 경계 품질, cold/warm 차이를 확인해야 한다. 이후 10/30/60/180초별 반복 생성의 재시도율·채택률·Pod 총 가동 청구 시간을 기록해야 한다.

현재 실측 GPU 데이터가 없으므로 **$269로 2026-11-17까지 500편을 제작할 수 있다고 결론내리지 않는다.** 예상 비용 필드는 관측 구간×시간당 단가이며 실제 청구액이 아니다.

## 변경 파일 안내

- 외부 연결: `ai_service/clients/comfyui.py`, `ai_service/clients/runpod.py`.
- 내부 입력: `ai_service/schemas/video.py`.
- 처리: `ai_service/services/video/{config,errors,images,planning,workflow,media,pipeline}.py`, `__init__.py`.
- 공식 자료: `ai_service/services/video/workflows/{wan2_2_flf2v.ui.json,wan2_2_flf2v.api.json,provenance.json,LICENSE}`.
- 실행/Mock: `scripts/video/{run,mock_comfyui}.py`, `__init__.py`.
- 테스트: `tests/video/test_{images,planning_workflow,runpod,pipeline}.py`, `__init__.py`.
- 의존성/설정: `pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore`.
- 문서: `README.md`, `docs/architecture.md`, `docs/video/{pipeline,validation}.md`.

기존 일정·음악·매칭·체크리스트 관련 처리 코드, HTTP 경로, SQS 메시지 계약 및 실제 `.env`는 수정하지 않았다.
