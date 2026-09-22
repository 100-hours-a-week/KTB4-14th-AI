"""Music contract and provider regressions; no paid or live API calls."""
import json
import unittest
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.errors import MusicRecommendationFailed, ServiceUnavailable
from ai_service.main import create_app
from ai_service.model import OpenAIPlanner
from ai_service.music import MusicCatalog
from ai_service.schemas import ItineraryRequest, ItineraryStreamRequest, MusicSuggestion
from test_day_transport import request


def record(title="Yellow", artist="Coldplay"):
    return {"kind": "song", "trackName": title, "artistName": artist, "trackId": 12345}


class MusicTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_song_uses_existing_keys_without_inventing_db_or_video_id(self):
        calls = []

        def handle(req):
            calls.append(req)
            return httpx.Response(200, json={"results": [record("Yellow (Live)"), record(artist="Cover Band"), record()]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            catalog = MusicCatalog(client)
            song = await catalog.verify(MusicSuggestion(title="YELLOW", artist="Coldplay"))
            self.assertEqual(song.title, "Yellow")
            self.assertEqual(song.artist, "Coldplay")
            self.assertEqual(set(song.model_dump()), {"title", "artist", "youtube_url"})
            url = urlparse(str(song.youtube_url))
            self.assertEqual(url.netloc, "www.youtube.com")
            self.assertEqual(url.path, "/results")
            self.assertEqual(parse_qs(url.query)["search_query"], ["Coldplay Yellow official audio"])
            self.assertEqual(calls[0].url.params["term"], "YELLOW Coldplay")
            self.assertNotIn("authorization", calls[0].headers)
            song.title = "caller mutation"
            cached = await catalog.verify(MusicSuggestion(title="Yellow", artist="Coldplay"))
            self.assertEqual(cached.title, "Yellow")
            self.assertEqual(len(calls), 1)

    async def test_unverified_cover_and_live_version_are_not_substituted(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"results": [record("Yellow (Live)"), record(artist="Cover Band")]})
        )) as client:
            self.assertIsNone(await MusicCatalog(client).verify(MusicSuggestion(title="Yellow", artist="Coldplay")))

    async def test_catalog_outage_or_invalid_envelope_is_503(self):
        for response in (httpx.Response(429), httpx.Response(500), httpx.Response(200, text="bad json"),
                         httpx.Response(200, json={"results": None})):
            with self.subTest(response=response):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as client:
                    with self.assertRaises(ServiceUnavailable) as error:
                        await MusicCatalog(client).verify(MusicSuggestion(title="Yellow", artist="Coldplay"))
                    self.assertEqual(error.exception.status_code, 503)

    async def test_free_recommendation_retries_unverified_song_with_context(self):
        model_calls, catalog_calls = [], []
        private_request = "바다를 바라보는 차분한 여행 private-marker"

        def handle(req):
            if req.method == "POST":
                model_calls.append(json.loads(req.content))
                suggestion = {"title": "Unverified" if len(model_calls) == 1 else "Yellow", "artist": "Coldplay"}
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(suggestion)}}]})
            catalog_calls.append(req)
            return httpx.Response(200, json={"results": [] if "Unverified" in req.url.params["term"] else [record()]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            planner = OpenAIPlanner(client, Settings(openai_api_key="test-only"))
            song = await planner.recommend_music(request(private_request))
        self.assertEqual(song.title, "Yellow")
        self.assertEqual(len(model_calls), 2)
        payload = json.loads(model_calls[0]["messages"][1]["content"])
        self.assertEqual(payload["preference"]["extra_request"], private_request)
        self.assertNotIn("candidates", payload)
        self.assertNotIn("music_candidates", payload)
        self.assertTrue(any(m["role"] == "assistant" and "Unverified" in m["content"] for m in model_calls[1]["messages"]))
        self.assertFalse(any("private-marker" in str(r.url) for r in catalog_calls))

    async def test_two_invalid_suggestions_fail_without_demo_fallback(self):
        for raw in ('{"title":"Unverified","artist":"Unknown"}', '{"music_id":1}'):
            calls = []

            def handle(req):
                if req.method == "POST":
                    calls.append(req)
                    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": raw}}]})
                return httpx.Response(200, json={"results": []})

            with self.subTest(raw=raw):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    with self.assertRaises(MusicRecommendationFailed) as error:
                        await OpenAIPlanner(client, Settings(openai_api_key="test-only")).recommend_music(request())
                self.assertEqual(error.exception.status_code, 422)
                self.assertEqual(len(calls), 2)


class MusicContractTests(unittest.TestCase):
    def test_stream_request_has_only_existing_itinerary_fields(self):
        fields = ItineraryStreamRequest.model_json_schema()["properties"]
        self.assertEqual(set(fields), set(ItineraryRequest.model_json_schema()["properties"]))
        data = request().model_dump(mode="json")
        self.assertEqual(ItineraryStreamRequest.model_validate(data).itinerary_request(), request())
        settings = Settings(api_token="test-only")
        with TestClient(create_app(settings=settings)) as client:
            schema = client.get("/openapi.json").json()["components"]["schemas"]["TravelGenerationRequest"]
            self.assertNotIn("music_candidates", schema["properties"])
            data["music_candidates"] = []
            response = client.post("/api/ai/v1/itinerary-jobs/stream", json=data,
                                   headers={"Authorization": "Bearer test-only"})
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
