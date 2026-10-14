# 동행자 매칭 개발 DB와 오프라인 평가

운영 AI 서버는 MySQL에 접속하지 않습니다. 백엔드가 조건과 후보를 조회해서
`POST /api/ai/v1/matches/candidates`에 `{request,candidates}`를 전달합니다.
[백엔드 호출 계약](backend-contract.md)에 따라 동기 HTTP 200 결과를 반환합니다.
ID 요청, 평면 매칭 요청, 매칭 큐 및 직접 DB 조회 API는 제공하지 않습니다.

## 개발 데이터

개발 DB `audigo_matching_backend_dev`에는 합성 사용자·프로필·요청 각각 1,000건과
백엔드 기준 지역 245건을 준비합니다. 지역은 현재 매칭 조건에 사용하지 않습니다.
테마는 NATURE/FOOD/CULTURE/REST/SNS/ACTIVITY, 속도는 RELAXED/BALANCED/PACKED입니다.
후보 프로필에는 예산·자기소개·여행 ID가 없습니다. 요청 예산을 후보와 비교하지 않습니다.

기준값은 `ai_service/matching/data/matching_backend_catalog.json`,
DB 구조는 `ai_service/matching/data/matching_dev_schema.sql`,
합성 데이터 생성은 `ai_service/matching/dev.py`를 참고하세요.

## 준비와 평가

```bash
uv sync --frozen --extra dev
./.venv/bin/python scripts/matching/setup_matching_dev_db.py
./.venv/bin/python scripts/matching/evaluate_matching_dev.py
```

준비 도구는 격리된 개발 DB만 만들며 기존 데이터가 다르면 덮어쓰지 않고 중단합니다.
관리자 암호는 터미널에서 입력합니다. 개발 데이터 계정은 `audigo_app`,
평가 테이블을 포함한 계정은 `audigo_seed`입니다. 기존 계정의 암호를 재설정하지 않습니다.
접속 정보는 Git에서 제외된 `.env.matching-dev`에 보관하며 API의 `.env`에는 쓰지 않습니다.
기존 DB·계정·과거 평가 파일은 이번 API 정리 과정에서 삭제하지 않았습니다.

평가 도구의 `scripts/matching/evaluation_data.py`만 개발 DB를 읽습니다.
하나의 읽기 전용 스냅샷에서 조건·활성 후보·평가 정답을 로드하고,
조건과 후보를 공개 백엔드 DTO로 변환해 공통 E5 순위 계산기를 실행합니다.
평가 정답은 AI 입력에 포함하지 않습니다. PyMySQL은 개발 의존성에만 포함됩니다.

DEV 600건에서 가중치를 선택한 뒤 TEST 300건과 EDGE 100건을 평가합니다.
DEV와 TEST의 요청자는 겹치지 않습니다. 합성 규칙 정답에 대한 Hit@3와 NDCG@3,
필수 조건 위반·적격 후보 누락을 기록합니다. 실제 사용자 수락률이나 성사율은 측정하지 않습니다.
`--apply`를 추가하면 DEV에서 선택한 E5·테마·속도 가중치를 로컬 `.env`에 반영합니다.
`--output`으로 별도 결과 폴더를 지정할 수 있습니다.

기존 `artifacts/matching/`의 결과는 이전 실행 기록이며, API 정리 후 평가를 다시 실행한 결과가 아닙니다.

## 검증

```bash
./.venv/bin/python -m unittest discover -s tests/matching -t . -v
```

테스트는 DTO·필터·최대 추천 수·오류·실제 로컬 E5 추론을 검사합니다.
이 테스트에 DB·Redis·외부 유료 API는 필요하지 않습니다.
