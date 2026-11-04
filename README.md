# AUDIGO AI

AUDIGO 여행 앱의 AI 서버입니다. 여행 일정을 짜 주고, 여행 사진으로 추억 영상을 만들어 줍니다.

## 바로 써 보기

설치 없이 브라우저에서 API를 눌러 볼 수 있습니다.

1. [API 테스트 화면](https://bggrh1188jif0b-8000.proxy.runpod.net/docs)을 엽니다.
2. 오른쪽 위 **Authorize**를 누르고 팀에서 받은 토큰(`AUDIGO_API_TOKEN`)을 넣습니다.
3. 써 볼 API를 열고 **Try it out** → **Execute**를 누릅니다.

## 내 컴퓨터에서 실행하기

[uv](https://docs.astral.sh/uv/getting-started/installation/)가 필요합니다. 없으면 먼저 설치합니다(Mac·Linux).

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

처음 한 번은 아래를 터미널에 그대로 붙여 넣습니다. 내려받기 → 설치 → 서버 실행까지 진행됩니다(음악 추천용 모델 약 250MB 포함).

```bash
git clone https://github.com/100-hours-a-week/KTB4-14th-AI.git
cd KTB4-14th-AI
uv sync --frozen
uv run python scripts/models/download_e5_model.py
[ -f .env ] || cp .env.example .env
uv run uvicorn ai_service.main:app --reload
```

다음부터는 프로젝트 폴더에서 서버만 실행합니다.

```bash
uv run uvicorn ai_service.main:app --reload
```

실행되면 [http://localhost:8000/docs](http://localhost:8000/docs)를 엽니다. 끌 때는 `Ctrl+C`를 누릅니다.
AI 기능을 쓰려면 `.env` 파일에 팀에서 받은 키를 채워야 합니다.

## 할 수 있는 것

| 기능 | 상태 |
| --- | --- |
| 여행 일정 만들기 | ✅ 사용 가능 |
| 여행 준비물 체크리스트 | ✅ 사용 가능 |
| 함께 갈 사람 추천 | ✅ 사용 가능 |
| 여행 음악 추천 | ✅ 사용 가능 |
| 사진 올리기 → 영상 요청 → 진행 확인 → 영상 보기 | ✅ 사용 가능 |
| AI가 실제로 영상 만들기 | ⏳ 준비 중 (영상용 GPU 연결 남음) |

## 더 알아보기

- [영상 API 사용법](docs/video/service-api.md)
- [개발자 안내](docs/developer-guide.md)
