# AUDIGO AI

여행 일정 생성 · 체크리스트 · 동행자 추천 · 음악 추천 · 여행 사진 AI 영상을 제공하는 FastAPI 서비스입니다.

## 빠른 실행

### 영상 서비스 테스트 화면 (설치 필요 없음)

영상 API는 RunPod CPU 서버 `audigo-ai-api-db`에 배포되어 있습니다.

👉 [영상 서비스 Swagger](https://bggrh1188jif0b-8000.proxy.runpod.net/docs)

1. 오른쪽 위 **Authorize**에 `.env`의 `AUDIGO_API_TOKEN` 값을 넣습니다.
2. 사용할 API에서 **Try it out** → `X-Owner-ID`에 `1`을 넣고 **Execute**를 누릅니다.
3. 사진 여러 장은 `사진 여러 장 업로드`에서 **Add string item**으로 칸을 늘려 한 장씩 고릅니다.

화면이 안 열리면 [RunPod Pods](https://console.runpod.io/pods)에서 서버가 **Running**인지 확인합니다.

터미널에서 업로드 → 영상 요청 → 진행 확인 → 재생 주소까지 한 번에 확인하려면 다음 명령을 실행합니다.

```bash
uv run python -m scripts.video.try_service --photos "사진폴더경로" --limit 12
```

### 내 맥에서 서버 실행

처음 한 번 (설치 + 실행):

```bash
cd /Users/samrobert/Documents/GitHub/KTB4-14th-AI
uv sync --frozen --no-dev --python /opt/homebrew/bin/python3.13
./.venv/bin/python scripts/models/download_e5_model.py model
[ -f .env ] || cp .env.example .env
./.venv/bin/python -m uvicorn ai_service.main:app --host 127.0.0.1 --port 8000 --reload
```

다음부터:

```bash
cd /Users/samrobert/Documents/GitHub/KTB4-14th-AI && ./.venv/bin/python -m uvicorn ai_service.main:app --host 127.0.0.1 --port 8000 --reload
```

실행 후 [http://localhost:8000/docs](http://localhost:8000/docs)를 엽니다. 종료는 `Ctrl+C`입니다.
로컬 서버에는 `VIDEO_DATABASE_URL`을 설정한 경우에만 영상 API가 표시됩니다.

테스트 실행:

```bash
uv run --extra dev pytest -q
```

## API와 진행 상황

모든 API는 `Authorization: Bearer <AUDIGO_API_TOKEN>`이 필요합니다(`/health` 제외).

| 기능 | Method | 경로 | 상태 |
| --- | --- | --- | --- |
| 서버 상태 | GET | `/health` | ✅ 완료 |
| 일정 생성 (JSON) | POST | `/internal/ai/itineraries/generate` | ✅ 완료 |
| 일정 생성 (실시간 SSE) | POST | `/api/ai/v1/itinerary-jobs/stream` | ✅ 완료 |
| 여행 체크리스트 | POST | `/api/ai/v1/checklists` | 🟡 구현, 백엔드 연동 확인 중 |
| 동행자 후보 추천 | POST | `/api/ai/v1/matches/candidates` | 🟡 구현, 백엔드 연동 확인 중 |
| 음악 추천 | POST | `/internal/ai/music/recommend` | 🟡 구현 |
| 사진 1장 업로드 | POST | `/api/ai/v1/uploads` | ✅ RunPod 배포·실서버 확인 |
| 사진 여러 장 업로드 | POST | `/api/ai/v1/uploads/batch` | ✅ RunPod 배포·실서버 확인 |
| 영상 생성 요청 | POST | `/api/ai/v1/video-jobs` | ✅ 접수·대기열 확인 / ⏳ GPU 생성 미검증 |
| 영상 진행 상태 | GET | `/api/ai/v1/video-jobs/{job_id}` | ✅ RunPod 배포·실서버 확인 |
| 내 영상 작업 목록 | GET | `/api/ai/v1/video-jobs` | ✅ RunPod 배포 |
| 영상 재생 주소 (5분 유효) | GET | `/api/ai/v1/video-jobs/{job_id}/video` | ✅ 브라우저 재생·탐색 확인 |

## 영상 서비스 현재 상태 (2026-11-04)

| 구성 | 위치 | 상태 |
| --- | --- | --- |
| API 서버 · 작업 실행기 · AI DB(PostgreSQL) | RunPod CPU Pod `bggrh1188jif0b`, US-WA-1 | ✅ 실행 중 |
| DB 복구용 기록(1분 단위)·매일 백업 | Network Volume `audigo-ai-service-data` (20GB) | ✅ 재시작 후 자동 복구 확인 |
| 사진 · 완성 MP4 · 썸네일 | Network Volume `audigo-video-models` (100GB) | ✅ |
| AI 영상 생성 | RunPod GPU Pod (현재 꺼짐) | ⏳ 연결·실제 생성 미검증 |

- ✅ 확인 완료: 사진 업로드, DB 기록, 작업 접수, 중복 요청 방지, 다른 사용자 접근 차단, 서버 재시작 후 데이터 유지, 재생 주소로 실제 영상 재생
- ⏳ 남은 일: GPU 서버 연결과 실제 영상 생성, GPU 자동 켜기/끄기 실사용 확인, 비밀 키를 RunPod Secrets로 이동
- GPU가 꺼져 있으면 영상 요청은 `GPU 대기` 상태로 안전하게 기다립니다.

## 자세한 문서

- [영상 서비스 API 사용법](docs/video/service-api.md)
- [개발자 상세 안내 (설치·영상 생성 실험·체크리스트·매칭·SSE 등 기존 README 전체)](docs/developer-guide.md)
- [프로젝트 구조와 수정할 파일 찾기](docs/architecture.md)
- [체크리스트 API](docs/checklist/api.md) · [동행자 매칭 계약](docs/matching/backend-contract.md) · [큐 작업](docs/sqs/queue.md)
