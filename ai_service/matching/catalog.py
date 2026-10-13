"""Matching values extracted from the backend development source.

Only the display labels are AI-owned translations. Enum values and regions
come from TravelThemeType/TravelPaceType/MatchingGender and backend data.sql.
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).with_name('data')
CATALOG = json.loads((DATA_DIR / 'matching_backend_catalog.json').read_text())
THEMES = tuple(CATALOG['themes'])
PACES = tuple(CATALOG['paces'])
GENDERS = tuple(CATALOG['genders'])
PREFERRED_GENDERS = tuple(CATALOG['preferred_genders'])
REGIONS = tuple(CATALOG['regions'])
LABELS = {'NATURE':'자연', 'FOOD':'음식', 'CULTURE':'문화', 'REST':'휴식',
          'SNS':'사진과 SNS', 'ACTIVITY':'활동적인 체험'}
