import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
import httpx
from ai_service.core.config import Settings
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.quality import review_people_fidelity

class PeopleFidelityTests(unittest.IsolatedAsyncioTestCase):
    async def decision(self,unmatched=(),uncertain=()):
        value={'reference_people':['A person seen from behind.'],'generated_foreground_people':['A visible traveler.'],'unmatched_foreground_people':list(unmatched),'uncertain_foreground_people':list(uncertain),'rationale':'Motion can be plausible while reference correspondence is missing.'}
        with patch('ai_service.services.video.quality.Settings.from_env',return_value=Settings(openai_api_key='test')),patch('ai_service.services.video.quality._review_images',return_value=[]),patch('ai_service.services.video.quality.OpenAIClient.complete',AsyncMock(return_value=json.dumps(value))):
            async with httpx.AsyncClient() as client:
                return await review_people_fidelity(client,VideoSettings(scene_model_review=True),Path('synthetic.mp4'),[],start_frame=0,frame_count=72)
    async def test_plausible_new_person_is_rejected(self):
        self.assertFalse((await self.decision(unmatched=['A new foreground person.']))['accepted'])
    async def test_uncertain_person_is_not_approved(self):
        self.assertFalse((await self.decision(uncertain=['An unverifiable facial close-up.']))['accepted'])
    async def test_supported_reference_people_can_pass(self):
        self.assertTrue((await self.decision())['accepted'])
