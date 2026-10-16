"""백엔드 개발 소스에서 추출한 매칭 기준값을 불러온다.

화면 표시용 명칭만 AI 쪽에서 번역한다. 열거형 값과 지역 정보는
백엔드의 TravelThemeType·TravelPaceType·MatchingGender와 data.sql을 따른다.
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / 'ai_service' / 'data' / 'matching'
CATALOG = json.loads((DATA_DIR / 'matching_backend_catalog.json').read_text())
THEMES = tuple(CATALOG['themes'])
PACES = tuple(CATALOG['paces'])
GENDERS = tuple(CATALOG['genders'])
PREFERRED_GENDERS = tuple(CATALOG['preferred_genders'])
REGIONS = tuple(CATALOG['regions'])
LABELS = {'NATURE':'자연', 'FOOD':'음식', 'CULTURE':'문화', 'REST':'휴식',
          'SNS':'사진과 SNS', 'ACTIVITY':'활동적인 체험'}
