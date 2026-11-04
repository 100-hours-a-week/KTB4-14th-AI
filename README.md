# AUDIGO AI

AUDIGO 여행 앱의 AI 서버입니다. 여행 일정을 짜 주고, 여행 사진으로 추억 영상을 만들어 줍니다.

## 바로 써 보기

설치 없이 브라우저에서 API를 눌러 볼 수 있습니다.

1. [API 테스트 화면](https://bggrh1188jif0b-8000.proxy.runpod.net/docs)을 엽니다.
2. 오른쪽 위 **Authorize**를 누르고 팀에서 받은 토큰(`AUDIGO_API_TOKEN`)을 넣습니다.
3. 써 볼 API를 열고 **Try it out** → **Execute**를 누릅니다.

## 내 컴퓨터에서 실행하기

```bash
uv sync --frozen
uv run uvicorn ai_service.main:app --reload
```

실행되면 [http://localhost:8000/docs](http://localhost:8000/docs)를 엽니다.
처음이라면 `.env.example`을 `.env`로 복사해 키를 채워야 합니다.

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
