# 백엔드 동행자 추천 호출 계약

백엔드 development의 MatchingCandidateAiRequest, DefaultMatchingAiClient,
MatchingAiCandidatesResponse 형식으로 AI 응답을 맞췄습니다.

## 기존 백엔드 호출

```http
POST /api/ai/v1/matching-requests
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
  ],
  "limit": 3
}
```

이 형태는 최종 추천을 HTTP 200으로 반환합니다. 계산식 기준 응답 예시:

```json
{"candidates":[{"user_id":23,"match_rate":100}],"count":1}
```

후보가 없거나 모두 필터링되면 HTTP 200과 `{"candidates":[],"count":0}`을 반환합니다.
응답에 message/data/job_id를 추가하지 않아 기존 DTO가 그대로 읽을 수 있습니다.

- 전달받은 실제 후보만 추천합니다. 로컬 목데이터 DB의 후보를 합치거나 대체하지 않습니다.
- 본인·선호 성별 불일치·속도가 양극단인 후보(`RELAXED ↔ PACKED`)를 제외합니다.
- 후보의 `user_id`, `nickname`, `gender`, `theme`, `pace`는 필수입니다. 누락·null·빈 테마 등
  불완전한 후보가 하나라도 있으면 전체 요청을 HTTP 400으로 거절합니다.
- 요청 테마는 1~3개, 후보 테마는 최대 4개이며 백엔드의 6개 enum만 허용합니다.
- 예산은 둘 다 없거나 `0 <= budget_min <= budget_max <= 3000000`이어야 합니다.
- 후보 프로필에 예산이 없으므로 요청 예산을 비교에 적용하지 않습니다. 예산 필터를 적용했다는
  의미로 해석하면 안 됩니다. 해당 정책은 기존 후보 데이터의 한계이며 실서비스 기준은 별도 합의가 필요합니다.
- `nickname`은 공백만 있는 값을 허용하지 않는 1~500자 문자열입니다. 앞뒤 공백은 보존합니다.
- `profile_image_url`은 선택 문자열이며 생략 또는 null을 허용합니다. 이미지는 점수에 사용하지 않으며 닉네임은 동점 정렬에만 사용합니다.
- `candidates`는 필수 배열이며 최대 10,000명입니다. 빈 배열은 정상적인 빈 추천을 반환합니다.
- 테마·속도 계산식 점수로 정렬하고 동점은 nickname 오름차순, 이후 user_id 오름차순입니다.
- 요청 최상위 `limit`은 선택 정수이며 기본 3, 허용 범위는 1~3입니다.
  0·4 이상·null·문자열·소수·boolean은 HTTP 400입니다. 실제 적격 후보가 적으면 그 수만 반환합니다.
- `match_rate`는 `80 × (겹치는 테마 수 / 요청 테마 수) + 속도 점수`를 반올림한 0~100 정수입니다.
  속도가 같으면 20점, 한 단계 차이면 10점입니다. 후보의 추가 테마는 감점하지 않습니다.
  실제 매칭 성사 확률이나 평가 적중률이 아닙니다.
- 오류는 입력 400, 인증 401, 서비스 토큰 미설정 503, 예상하지 못한 내부 오류 500입니다.

이 호출에는 Redis·Celery·AI의 MySQL 접속·OpenAI/카카오 키가 필요하지 않습니다.
백엔드가 자신의 DB에서 조건과 후보를 조회해서 전달하고 AI는 테마·속도 계산식으로 비교합니다.
AI가 백엔드 대신 사용자 인증이나 요청 소유권을 결정하지 않습니다.

## 백엔드 실행 설정

```dotenv
AUDIGO_AI_BASE_URL=http://AI서버주소:8000
AUDIGO_AI_MATCHING_CANDIDATES_PATH=/api/ai/v1/matching-requests
AUDIGO_AI_MATCHING_MOCK_ENABLED=false
AUDIGO_API_TOKEN=양쪽에_동일하게_설정한_서비스_토큰
```

새 코드를 반영해 서버를 재시작해야 합니다. 동행자 매칭에는 모델 파일이 필요하지 않습니다.
위 변경으로 기존 Java 요청·응답 코드를 바꿀 필요는 없습니다. 실배포의 DNS·네트워크·토큰은 해당
환경에서 확인해야 하며 전체 Spring 앱·프론트·운영 DB 연동을 실행한 검증은 아닙니다.

## 지원하는 요청 형식

`{request,candidates}` 형식의 동기 HTTP 200 계약만 지원합니다.
ID만 전달하는 요청과 이전 평면 요청은 HTTP 400 입력 오류를 반환합니다.
매칭 작업 접수·상태 조회·AI의 직접 DB 조회 기능은 제거했습니다.
백엔드가 DB 조회와 후보 구성을 담당하며 AI는 전달받은 후보의 순위를 계산합니다.

## 구현 위치

- ai_service/matching/backend.py: 백엔드 요청·응답 DTO 스키마.
- ai_service/api/routes/matching.py: 백엔드 DTO를 받는 동기 추천 경로.
- ai_service/matching/scoring.py: 테마 80점·속도 20점, 양극단 제외, 정수 반올림.
- ai_service/matching/service.py: 본인·성별 제외, 점수·닉네임·ID 정렬, limit 적용.

## 검증과 재현

```bash
./.venv/bin/python -m unittest discover -s tests/matching -t . -p 'test_matching_backend.py' -v
./.venv/bin/python scripts/matching/check_matching_backend_client.py --backend-source /Users/samrobert/Downloads/KTB4-14th-BE-development.zip
```

두 번째 명령은 백엔드의 원래 Java 클라이언트·DTO·관련 엔티티 소스를 변경 없이 컴파일하고,
임시 포트의 실제 AI 서버를 호출합니다. JDK와 백엔드 의존성의 로컬 Gradle 캐시가 필요합니다.
모델 추론 없이 계산식을 사용하며 기존 서버·DB·Redis를 변경하지 않습니다.
추천, 빈 후보, 닉네임 동점과 본인 제외의 세 사례에서 HTTP 200 및 Java DTO 역직렬화를 확인했습니다.
[Java 클라이언트 검증 기록](../../artifacts/matching/theme-pace-v1/backend-client-smoke.json).
