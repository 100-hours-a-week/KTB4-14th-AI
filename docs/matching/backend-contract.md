# 백엔드 동행자 추천 호출 계약

백엔드 development의 MatchingCandidateAiRequest, DefaultMatchingAiClient,
MatchingAiCandidatesResponse 형식으로 AI 응답을 맞췄습니다.

## 기존 백엔드 호출

```http
POST /api/ai/v1/matches/candidates
Authorization: Bearer <AUDIGO_API_TOKEN>
Content-Type: application/json
```

```json
{
  "request": {
    "matching_request_id": 10,
    "user_id": 1,
    "preferred_companion_gender": "FEMALE",
    "theme": ["NATURE", "FOOD"],
    "pace": "BALANCED",
    "budget_min": 100000,
    "budget_max": 800000
  },
  "candidates": [
    {"user_id": 22, "nickname": "매칭후보A", "gender": "MALE", "theme": ["CULTURE", "SNS"], "pace": "RELAXED"},
    {"user_id": 23, "nickname": "매칭후보B", "gender": "FEMALE", "theme": ["NATURE", "FOOD"], "pace": "BALANCED"}
  ]
}
```

이 형태는 최종 추천을 HTTP 200으로 반환합니다. 실제 Java 클라이언트 검증의 결과:

```json
{"candidates":[{"user_id":23,"match_rate":98}],"count":1}
```

후보가 없거나 모두 필터링되면 HTTP 200과 `{"candidates":[],"count":0}`을 반환합니다.
응답에 message/data/job_id를 추가하지 않아 기존 DTO가 그대로 읽을 수 있습니다.

- 전달받은 실제 후보만 추천합니다. 로컬 목데이터 DB의 후보를 합치거나 대체하지 않습니다.
- 본인·선호 성별 불일치·성별/속도/테마가 없는 후보를 제외합니다.
- 요청 테마는 1~3개, 후보 테마는 최대 4개이며 백엔드의 6개 enum만 허용합니다.
- 예산은 둘 다 없거나 `0 <= budget_min <= budget_max <= 3000000`이어야 합니다.
- 후보 프로필에 예산이 없으므로 요청 예산을 비교에 적용하지 않습니다. 예산 필터를 적용했다는
  의미로 해석하면 안 됩니다. 해당 정책은 기존 후보 데이터의 한계이며 실서비스 기준은 별도 합의가 필요합니다.
- E5/테마/속도 점수로 정렬하고 동점은 nickname 오름차순, 이후 user_id 오름차순입니다.
- 기본 추천 수는 3, 서버 설정 `AUDIGO_AI_MATCHING_CANDIDATE_LIMIT`로 1~20 범위 조정합니다.
  현재 백엔드는 추천 수를 Body에 넣지 않으므로 양쪽 서버 설정을 동일하게 맞춥니다.
- `match_rate`는 0~100 정수 표시 점수입니다. 순수 코사인일 때 `(cosine+1)/2 * 100`,
  조합 점수일 때 `score * 100`을 반올림합니다. 실제 매칭 성사 확률이나 평가 적중률이 아닙니다.
- 오류는 입력 400, 인증 401, 모델 누락·추론 실패·시간 초과 503입니다.

이 호출에는 Redis·Celery·AI의 MySQL 접속·OpenAI/카카오 키가 필요하지 않습니다.
백엔드가 자신의 DB에서 조건과 후보를 조회해서 전달하고 AI는 로컬 E5로 비교합니다.
AI가 백엔드 대신 사용자 인증이나 요청 소유권을 결정하지 않습니다.

## 백엔드 실행 설정

```dotenv
AUDIGO_AI_BASE_URL=http://AI서버주소:8000
AUDIGO_AI_MATCHING_CANDIDATES_PATH=/api/ai/v1/matches/candidates
AUDIGO_AI_MATCHING_MOCK_ENABLED=false
AUDIGO_AI_MATCHING_CANDIDATE_LIMIT=3
AUDIGO_API_TOKEN=양쪽에_동일하게_설정한_서비스_토큰
```

AI에 고정 E5 모델을 준비하고 새 코드를 반영해 서버를 재시작해야 합니다.
위 변경으로 기존 Java 요청·응답 코드를 바꿀 필요는 없습니다. 실배포의 DNS·네트워크·토큰은 해당
환경에서 확인해야 하며 전체 Spring 앱·프론트·운영 DB 연동을 실행한 검증은 아닙니다.

## 매칭 큐 API 제거

동행자 매칭은 `POST /api/ai/v1/matches/candidates`의 동기 200 응답만 사용합니다.
이전 `POST /api/ai/v1/traveler-match-jobs` 경로는 제거되었습니다(404).
ID만 전달하거나 중첩되지 않은 후보 목록을 전달하는 이전 큐 요청은 더 이상 허용하지 않습니다(400).
매칭 API는 `Idempotency-Key`를 받지 않으며 Redis·Celery·AI MySQL에 의존하지 않습니다.
일정 생성 작업 접수 및 조회 API는 유지합니다.

## 구현 위치

- ai_service/matching/backend.py: Java DTO 스키마, 기존 추천기 입력 변환, 결과 DTO 변환.
- ai_service/api/routes/matching.py: 백엔드 DTO만 수신하고 E5 추천 결과를 즉시 반환.
- matching.py: 공통 E5 순위 계산, 동점 기준을 선택 가능하게 확장.
- main.py/config.py: 모델 캐시와 추천 수·가중치 설정 연결.

## 검증과 재현

```bash
./.venv/bin/python -m unittest discover -s tests/matching -t . -p 'test_matching_backend.py' -v
./.venv/bin/python scripts/matching/check_matching_backend_client.py --backend-source /Users/samrobert/Downloads/KTB4-14th-BE-development.zip
```

두 번째 명령은 백엔드의 원래 Java 클라이언트·DTO·관련 엔티티 소스를 변경 없이 컴파일하고,
임시 포트의 실제 AI 서버를 호출합니다. JDK와 백엔드 의존성의 로컬 Gradle 캐시가 필요합니다.
고정 로컬 E5를 사용하며 기존 서버·DB·Redis를 변경하지 않습니다.
추천, 빈 후보, 닉네임 동점과 본인 제외의 세 사례에서 HTTP 200 및 Java DTO 역직렬화를 확인했습니다.
[Java 클라이언트 검증 기록](../../artifacts/matching/backend-client-smoke.json).
