# 백엔드 동행자 추천 호출 계약

백엔드 development의 MatchingCandidateAiRequest, DefaultMatchingAiClient,
MatchingAiCandidatesResponse 형식을 지원합니다. 후보별 선택 예산 필드를 추가 지원하며,
기존 백엔드처럼 후보 예산을 생략한 요청도 처리합니다.

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
    {"user_id": 23, "nickname": "매칭후보B", "gender": "FEMALE", "theme": ["NATURE", "FOOD"], "pace": "BALANCED", "budget_min": 100000, "budget_max": 800000}
  ],
  "limit": 3
}
```

[목 프로필 기반 복사용 요청 JSON](candidates-request.json.example)을 Swagger 본문에 사용할 수 있습니다.
닉네임과 매칭 요청 ID·예산은 설명용 값입니다. 기존 목 프로필의 값은 대문자 enum으로 변환하고,
지원하지 않는 이전 테마 `city`는 제외했습니다. 원본 목데이터 파일은 변경하지 않습니다.

AI 매칭 API는 목데이터를 자동 조회하거나 추천 결과를 DB에 저장하지 않습니다.
백엔드가 전달한 후보를 계산해 응답하며, 서비스 DB에 결과를 저장하는 책임은 백엔드에 있습니다.
개발 DB의 합성 사용자·평가 데이터와 오프라인 평가 파일은 운영 추천 결과 저장과 구분합니다.

이 형태는 최종 추천을 HTTP 200으로 반환합니다. 계산식 기준 응답 예시:

```json
{"candidates":[{"user_id":23,"match_rate":100}],"count":1}
```

요청 목록의 남성 `22`는 선호 성별 `FEMALE`과 달라 제외하고 여성 `23`만 추천합니다.

후보가 없거나 모두 필터링되면 HTTP 200과 `{"candidates":[],"count":0}`을 반환합니다.
응답에 message/data/job_id를 추가하지 않아 기존 DTO가 그대로 읽을 수 있습니다.

- 전달받은 실제 후보만 추천합니다. 로컬 목데이터 DB의 후보를 합치거나 대체하지 않습니다.
- 본인·선호 성별 불일치·속도가 양극단인 후보(`RELAXED ↔ PACKED`)를 제외합니다.
- 후보의 `user_id`, `nickname`, `gender`, `theme`, `pace`는 필수입니다. 누락·null·빈 테마 등
  불완전한 후보가 하나라도 있으면 전체 요청을 HTTP 400으로 거절합니다.
- 요청 테마는 1~3개, 후보 테마는 최대 4개이며 백엔드의 6개 enum만 허용합니다.
- 예산은 둘 다 없거나 `0 <= budget_min <= budget_max <= 3000000`이어야 합니다.
- 후보의 `budget_min`, `budget_max`는 선택 필드입니다. 요청과 동일한 범위·쌍 검증을 적용합니다.
- 요청과 후보 양쪽 예산이 있으면 겹치는 정도에 따라 최대 5점만 감점합니다. 예산으로 후보를 제외하지 않습니다.
- 어느 한쪽 예산이 없으면 비교하지 않으며 기존 테마·속도 점수를 유지합니다. 누락된 예산을 임의로 추정하지 않습니다.
- 현재 백엔드 `CandidateProfilePayload`에는 예산 필드가 없습니다. 실제 데이터에 예산을 반영하려면
  백엔드가 출처가 확인된 후보 예산을 조회해 두 필드로 전달해야 합니다. AI의 개발 DB·목데이터는 자동 조회하지 않습니다.
- `nickname`은 공백만 있는 값을 허용하지 않는 1~500자 문자열입니다. 앞뒤 공백은 보존합니다.
- `profile_image_url`은 선택 문자열이며 생략 또는 null을 허용합니다. 이미지는 점수에 사용하지 않으며 닉네임은 동점 정렬에만 사용합니다.
- `candidates`는 필수 배열이며 최대 10,000명입니다. 빈 배열은 정상적인 빈 추천을 반환합니다.
- 테마·속도 계산식 점수로 정렬하고 동점은 nickname 오름차순, 이후 user_id 오름차순입니다.
- 요청 최상위 `limit`은 선택 정수이며 기본 3, 허용 범위는 1~3입니다.
  0·4 이상·null·문자열·소수·boolean은 HTTP 400입니다. 실제 적격 후보가 적으면 그 수만 반환합니다.
- 기본 점수는 `80 × (겹치는 테마 수 / 요청 테마 수) + 속도 점수`입니다.
  속도가 같으면 20점, 한 단계 차이면 10점입니다. 후보의 추가 테마는 감점하지 않습니다.
- 두 예산 범위 중 짧은 범위를 기준으로 `겹치는 금액 길이 / 짧은 범위 길이`를 계산합니다.
  짧은 범위가 완전히 포함되면 1, 전혀 겹치지 않으면 0이며, 감점은 `5 × (1 - 겹침 비율)`입니다.
  고정 금액(최소=최대)은 상대 범위 안에 있으면 1, 밖이면 0입니다. 0원도 정상 비교합니다.
- 최종 점수는 기본 점수에서 예산 감점을 뺀 뒤 한 번만 반올림(0.5 올림)한 0~100 정수입니다.
  같은 테마·속도의 기본 점수 100점에서 예산이 완전히 겹치면 100점, 절반 겹치면 98점, 겹치지 않으면 95점입니다.
  `match_rate`는 실제 매칭 성사 확률이나 평가 적중률이 아닙니다.
- 오류는 입력 400, 인증 401, 서비스 토큰 미설정 503, 예상하지 못한 내부 오류 500입니다.

이 호출에는 Redis·Celery·AI의 MySQL 접속·OpenAI/카카오 키가 필요하지 않습니다.
백엔드가 자신의 DB에서 조건과 후보를 조회해서 전달하고 AI는 테마·속도 계산식으로 비교합니다.
AI가 백엔드 대신 사용자 인증이나 요청 소유권을 결정하지 않습니다.

## 백엔드 실행 설정

```dotenv
AUDIGO_AI_BASE_URL=http://AI서버주소:8000
AUDIGO_AI_MATCHING_CANDIDATES_PATH=/api/ai/v1/matches/candidates
AUDIGO_AI_MATCHING_MOCK_ENABLED=false
AUDIGO_API_TOKEN=양쪽에_동일하게_설정한_서비스_토큰
```

새 코드를 반영해 서버를 재시작해야 합니다. 동행자 매칭에는 모델 파일이 필요하지 않습니다.
후보 예산을 생략하는 기존 Java 요청·응답은 그대로 사용할 수 있습니다. 예산 비교를 사용하려면 후보 DTO와 조회 로직 보완이 필요합니다. 실배포의 DNS·네트워크·토큰은 해당
환경에서 확인해야 하며 전체 Spring 앱·프론트·운영 DB 연동을 실행한 검증은 아닙니다.

## Mock 모드와 실제 호출 확인

`AUDIGO_AI_MATCHING_MOCK_ENABLED`는 AI 서버가 아닌 백엔드 설정입니다.
`true`면 백엔드가 AI HTTP 호출을 생략하고 입력 순서의 앞 후보에 92·85·78점 등의 임시 점수를 붙입니다.
이 경로에는 AI의 성별·속도 필터나 예산 감점이 실행되지 않습니다. 실제 추천 검증에는 `false`가 필요합니다.
백엔드 코드의 기본값은 `false`지만 운영 환경변수·배포 설정이 덮어쓸 수 있으므로 코드만으로 운영 상태를 단정하지 않습니다.

백엔드 로그의 `matching_ai_candidates_mocked`는 임시 응답, `matching_ai_candidates_request_start`와
`matching_ai_candidates_request_complete`는 실제 HTTP 호출 경로입니다. 요청 시작만 있고 완료가 없으면 성공으로 판단하지 않습니다.
설정 변경은 백엔드 실행 환경에 적용하고 재시작해야 합니다. 이 작업에서는 운영 환경을 변경하지 않습니다.

## 지원하는 요청 형식

`{request,candidates}` 형식의 동기 HTTP 200 계약만 지원합니다.
ID만 전달하는 요청과 이전 평면 요청은 HTTP 400 입력 오류를 반환합니다.
매칭 작업 접수·상태 조회·AI의 직접 DB 조회 기능은 제거했습니다.
백엔드가 DB 조회와 후보 구성을 담당하며 AI는 전달받은 후보의 순위를 계산합니다.

## 구현 위치

- ai_service/schemas/matching.py: 백엔드 요청·응답 DTO 스키마.
- ai_service/api/routes/matching.py: 백엔드 DTO를 받는 동기 추천 경로.
- ai_service/services/matching.py: 본인·성별·속도 필터, 테마 80점·속도 20점·예산 최대 5점 감점, 정렬·limit 적용.

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
