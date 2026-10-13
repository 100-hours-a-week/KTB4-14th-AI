# 백엔드 development 기준 동행자 매칭 DB

## 적용 범위와 근거

백엔드 `development` 소스, 커밋
`75d7772b03ecc265527e27c78863b0f457761841`의 엔티티·Repository·enum·data.sql을 기준으로
AI 매칭 목데이터와 조회 SQL을 맞췄습니다. 일정 생성 코드는 이번 변경 대상이 아닙니다.
DB 조회와 함께 백엔드의 기존 동기 요청·응답 계약도 지원합니다. 원래 Java 클라이언트의 실제 HTTP 호출을
검증했으며, 전체 Spring 앱 및 실제 배포 DB 통합 실행은 아닙니다.

| 구분 | 백엔드에서 가져온 값 |
| --- | --- |
| 테마 | NATURE, FOOD, CULTURE, REST, SNS, ACTIVITY |
| 속도 | RELAXED, BALANCED, PACKED |
| 프로필 성별 | FEMALE, MALE |
| 선호 성별 | ANY, FEMALE, MALE |
| 지역 | data.sql의 name/full_name 245개 항목 |

`ai_service/data/matching_backend_catalog.json`에 원본 파일 경로·SHA-256·커밋을 함께 기록합니다.
`matching_catalog.py`는 이 파일에서 값을 읽습니다. LABELS는 이 enum의 문장 표현용 번역이며
새로운 테마를 정의하지 않습니다. 별도 ENGLISH 소개 목록은 제거했습니다.

백엔드 소스에서 같은 기준값을 다시 추출하려면:

```bash
./.venv/bin/python scripts/extract_matching_backend_catalog.py --backend-zip /Users/samrobert/Downloads/KTB4-14th-BE-development.zip
```

백엔드 소스 버전이 달라지면 enum·DDL·조회·검증도 함께 확인해야 합니다. 기존 DB를 자동 덮어쓰지 않습니다.
지역은 백엔드 초기 데이터에 맞춰 넣지만 매칭의 조회·순위에는 사용하지 않습니다.
실제 서버의 region ID가 로컬 초기화 순서와 같다고 가정하지 않습니다.

## 새 개발 DB

현재 로컬 DB: `audigo_matching_backend_dev`.
이전 임의 스키마의 `audigo_matching_dev`와 데이터는 덮어쓰지 않습니다.

- 사용자 1,000명, 매칭 프로필 1,000건, 매칭 요청 1,000건.
- 한 사용자당 한 요청. 프로필 테마 최대 4개, 요청 테마 1~3개, 예산은 둘 다 없거나
  `0 <= budget_min <= budget_max <= 3,000,000`을 만족합니다.
- 프로필 비활성·미확인 성별·빈 테마를 포함해 불완전 후보 배제를 검사합니다.
- 기존 목데이터의 사용자 ID와 백엔드에서 허용하는 값만 가져옵니다. 과거의 CITY/BEACH/RELAX,
  후보 예산·소개 등은 새 DB 데이터에 복제하지 않습니다.
- 업무 테이블은 users, matching_profiles, matching_profile_themes, matching_requests,
  matching_requests_themes, regions입니다.
- 매칭 요청에는 travel_plan_id, 지역, 기간, OPEN 상태를 추가하지 않습니다.
- 매칭 프로필에는 후보 예산, 소개, 임의 차단 컬럼을 추가하지 않습니다.
- 테이블·컬럼·FK·고유키는 대응하는 백엔드 엔티티 기준이며, 로컬 SQL의 CHECK는 Java 검증을
  개발 DB에서도 검사하기 위한 보조 제약입니다. 실제 JPA 자동 생성 DDL과 완전히 동일한 덤프는 아닙니다.
- matching_evaluation_cases/labels/dataset_meta는 AI 개발 평가 전용입니다. 백엔드나 런타임에
  해당 테이블을 추가할 필요는 없습니다.

계정은 `audigo_ai_reader`(업무 SELECT), `audigo_app`(업무 DML), `audigo_seed`(업무·평가 DML)입니다.
새 DB에서도 AI reader는 정답 조회와 쓰기가 불가능합니다. 로컬 계정은 localhost 전용입니다.
암호는 git에서 제외한 `.env.matching-dev`에 0600으로 저장하고 reader 연결만 `.env`에 적용합니다.

```bash
./.venv/bin/python scripts/setup_matching_dev_db.py
```

관리자 암호는 터미널에서 입력합니다. 기본 DB는 audigo_matching_backend_dev입니다.
같은 버전은 중복 삽입 없이 재사용합니다. 이전 데이터 버전·알 수 없는 DB·운영 DB 이름은
덮어쓰지 않습니다. root는 구성용이며 런타임은 reader를 사용합니다.

## 실제 SQL과 처리

매핑 파일: `ai_service/data/matching_dev_queries.json`.

1. context: matching_requests에서 요청 ID·요청자 ID를 함께 확인하고,
   matching_requests_themes를 JSON 테마 배열로 모읍니다.
2. candidates: matching_profiles와 users를 JOIN하고 matching_profile_themes를 배열로 읽습니다.
   백엔드 Repository/isComplete와 같이 is_active, 본인 제외, 성별·속도·테마 완성 여부를 검사합니다.
3. 전체 후보를 동일한 READ ONLY / REPEATABLE READ 스냅샷에서 201명씩 읽습니다.
4. AI가 선호 성별을 적용하고 E5로 최대 200명씩 임베딩한 뒤 전체 후보에서 상위 K명을 선택합니다.
   최대 10,000명 초과 시 일부만 추천하지 않고 MATCH_CANDIDATE_LIMIT_EXCEEDED를 반환합니다.

예산은 요청 조건으로 읽어 보존하지만 백엔드 후보 프로필에는 비교할 예산이 없습니다.
이를 0원·요청자 예산 등으로 채우지 않으며, 예산 때문에 모든 후보를 제외하지도 않습니다.
예산 조건이 있었으면 큐 완료 결과에 `unapplied_conditions: ["budget"]`를 포함합니다.
없으면 빈 배열입니다. 예산이 필수 필터여야 하는 제품 정책이라면 후보 데이터 기준을 별도로 합의해야 합니다.
지역·기간·차단·소개도 현재 백엔드 매칭에 존재하는 조건인 것처럼 처리하지 않습니다.

## AI에서 직접 검증하는 요청

```http
POST /api/ai/v1/traveler-match-jobs
Authorization: Bearer <AUDIGO_API_TOKEN>
Content-Type: application/json
```

```json
{"matching_request_id":201,"requester_id":201,"limit":3}
```

202의 status_url을 같은 토큰으로 조회합니다. COMPLETED.result에는 matching_request_id,
requester_id, recommendations(user_id/score/rank), unapplied_conditions가 있습니다.
이전 `/api/ai/v1/matches/candidates`의 ID 요청도 202로 호환 처리합니다.
DB 조회에 travel_plan_id를 넣으면 현재 매핑에서는 INVALID_REQUEST입니다.
기존 사용자 정의 여행 연계 매핑에 한해서 선택 travel_plan_id를 계속 지원합니다.
입력에 candidates가 있는 기존 큐 요청과 동기 `/matching-requests` 목데이터 데모는 호환용으로 유지합니다.

SQL은 서버 설정 파일에서만 읽고 HTTP Body로 받지 않습니다. 값은 PyMySQL 파라미터로 바인딩합니다.
DB 장애는 상태의 FAILED/RETRYING로 확인하고, 요청·사용자 불일치는 MATCHING_REQUEST_NOT_FOUND,
후보 없음은 NO_MATCH_CANDIDATES입니다. 가상 후보로 대체하지 않습니다.

## E5와 평가

백엔드 테마·속도를 한국어 문장으로 변환하고 query:/passage: 접두어를 사용합니다.
프로필에 없는 소개는 만들지 않습니다. 성별은 필터링하며 정답·ID는 문장에 포함하지 않습니다.
기본 가중치 (1,0,0)은 순수 코사인을 유지합니다. 선택 가중치는
`MATCHING_E5_WEIGHT`, `MATCHING_THEME_WEIGHT`, `MATCHING_PACE_WEIGHT`이며 합이 1이어야 합니다.
조합 점수는 E5 정규화 유사도 + 테마 Jaccard + 속도 일치도입니다. score는 성공 확률이 아닙니다.

새 평가: 백엔드의 허용 값에서 요청을 독립적으로 무작위 생성합니다. 개발 600건으로 가중치를
선택한 후 요청자가 겹치지 않는 300건에서 Hit@3/NDCG@3/Precision@3를 평가합니다.
예외 입력 100건도 별도로 검사합니다. 모두 합성 데이터이며 실제 수락률이 아닙니다.
기존 스키마의 Hit@5 보고서는 `artifacts/matching/legacy-v1/`에 과거 기록으로 보관하고 새 결과에 재사용하지 않습니다.

```bash
./.venv/bin/python scripts/evaluate_matching_dev.py --apply
```

[최신 보고서](../artifacts/matching/evaluation-report.md), [카탈로그](../ai_service/data/matching_backend_catalog.json).
설정 변경 후 API·worker를 재시작합니다. 임베딩 캐시는 오프라인 평가용이며 실행 시간은 실서비스 지연이 아닙니다.

## 실제 백엔드 호출

백엔드의 `/api/ai/v1/matches/candidates` `{request,candidates}` 요청에는 이제 동기 HTTP 200과
`{candidates:[{user_id,match_rate}],count}`를 반환합니다. 원래 DefaultMatchingAiClient와 DTO를
수정 없이 컴파일해 실제 로컬 AI HTTP 호출·응답 파싱까지 검증했습니다.
[요청·응답·점수·실행 계약](matching-backend-contract.md)을 참고하세요.

이 동기 호출은 BE가 보내는 후보를 사용하므로 AI의 MySQL·Redis 설정이 필요하지 않습니다.
이 문서의 ID 기반 큐 호출을 사용할 때에만 실제 DB와 Redis·worker·beat 설정이 필요합니다.
MySQL은 MATCHING_MYSQL_HOST/PORT/DATABASE/USER/PASSWORD/QUERY_FILE로 연결합니다.
Docker localhost는 컨테이너 자신입니다. 호스트 DB 주소·접속 허용 계정은 배포 환경에 맞추고,
이미지 내 매핑은 /app/ai_service/data/matching_dev_queries.json, 원격 TLS는 MATCHING_MYSQL_SSL_CA입니다.

## 테스트

```bash
./.venv/bin/python -m unittest discover -s tests -p 'test_matching_dev.py' -v
MYSQLD_BINARY=/usr/local/mysql/bin/mysqld REDIS_SERVER=/경로/redis-server ./.venv/bin/python -m unittest discover -s tests -p 'test_matching_mysql_integration.py' -v
```

원본 enum/data.sql 일치, 허용 값·필드, 실제 SQL 1,000건, FK·enum 제약, 조회 권한,
999명 페이지 조회·테마 중복 제거·미확인 예산·불완전 후보와 실제 Redis/Celery/E5 결과를 검사합니다.
통합 테스트는 임시 DB·Redis를 별도 포트에 구성하며 기존 DB를 변경하지 않습니다.
