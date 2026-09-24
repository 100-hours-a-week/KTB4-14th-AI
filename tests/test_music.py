"""Music provider regressions; no paid or live API calls."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient
from ai_service.config import Settings
from ai_service.errors import MusicRecommendationFailed, ServiceUnavailable
from ai_service.main import create_app
from ai_service.model import OpenAIPlanner
from ai_service.music import YouTubeMusic
from ai_service.schemas import ItineraryRequest, ItineraryStreamRequest, MusicSuggestion
from test_day_transport import request

VIDEO_ID = 'abcdefghijk'
SUGGESTION = MusicSuggestion(title='Yellow', artist='Coldplay')


def record(title='Coldplay - Yellow (Official Video)', author='Coldplay', video_id=VIDEO_ID):
    return {'id': video_id, 'title': title, 'channel': author}


def metadata(title='Coldplay - Yellow (Official Video)', author='Coldplay'):
    return {'type': 'video', 'title': title, 'author_name': author}


class MusicTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_video_existing_keys_and_cache(self):
        calls = []
        def handle(req):
            calls.append(req)
            self.assertEqual(req.url.host, 'www.youtube.com')
            self.assertEqual(req.url.path, '/oembed')
            return httpx.Response(200, json=metadata())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            resolver = YouTubeMusic(client)
            resolver._search = AsyncMock(return_value=[record()])
            song = await resolver.find_video(SUGGESTION)
            self.assertEqual(set(song.model_dump()), {'title', 'artist', 'youtube_url'})
            self.assertEqual(song.title, 'Yellow')
            url = urlparse(str(song.youtube_url))
            self.assertEqual(url.path, '/watch')
            self.assertEqual(parse_qs(url.query), {'v': [VIDEO_ID]})
            resolver._search.assert_awaited_once_with('Coldplay Yellow official audio')
            song.title = 'caller mutation'
            self.assertEqual((await resolver.find_video(SUGGESTION)).title, 'Yellow')
            self.assertEqual(len(calls), 1)

    async def test_covers_live_invalid_ids_and_deleted_videos_are_skipped(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(404))) as client:
            resolver = YouTubeMusic(client)
            resolver._search = AsyncMock(return_value=[
                record('Coldplay - Yellow (Live)'), record('Yellow', 'Cover Band'),
                record('Coldplay - Yellow Cover'), record('Coldplay - Yellowstone'), record(video_id='invented-url'),
                dict(record(), live_status='is_live'), record(),
            ])
            self.assertIsNone(await resolver.find_video(SUGGESTION))

    async def test_oembed_must_also_match_song(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=metadata('Another song', 'Another singer'))
        )) as client:
            resolver = YouTubeMusic(client)
            resolver._search = AsyncMock(return_value=[record()])
            self.assertIsNone(await resolver.find_video(SUGGESTION))

    async def test_outage_and_malformed_metadata_are_503(self):
        for response in (httpx.Response(429), httpx.Response(500),
                         httpx.Response(200, text='bad json'), httpx.Response(200, json=[])):
            with self.subTest(response=response):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as client:
                    resolver = YouTubeMusic(client)
                    resolver._search = AsyncMock(return_value=[record()])
                    with self.assertRaises(ServiceUnavailable):
                        await resolver.find_video(SUGGESTION)

    async def test_recommendation_retries_without_external_travel_data(self):
        model_calls, searches = [], []
        private_request = '바다를 바라보는 차분한 여행 private-marker'
        def handle(req):
            if req.method == 'POST':
                model_calls.append(json.loads(req.content))
                suggestion = {'title': 'Unverified' if len(model_calls) == 1 else 'Yellow', 'artist': 'Coldplay'}
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(suggestion)}}]})
            self.assertEqual(req.url.host, 'www.youtube.com')
            return httpx.Response(200, json=metadata())
        async def search(query):
            searches.append(query)
            return [] if 'Unverified' in query else [record()]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            planner = OpenAIPlanner(client, Settings(openai_api_key='test-only'))
            planner.youtube_music._search = search
            song = await planner.recommend_music(request(private_request))
        self.assertEqual(song.title, 'Yellow')
        self.assertEqual(len(model_calls), 2)
        payload = json.loads(model_calls[0]['messages'][1]['content'])
        self.assertEqual(payload['preference']['extra_request'], private_request)
        self.assertNotIn('candidates', payload)
        self.assertTrue(any(m['role'] == 'assistant' and 'Unverified' in m['content'] for m in model_calls[1]['messages']))
        self.assertFalse(any('private-marker' in query for query in searches))

    async def test_two_invalid_suggestions_fail_without_fallback(self):
        for raw in ('{"title":"Unverified","artist":"Unknown"}', '{"music_id":1}'):
            calls = []
            def handle(req):
                calls.append(req)
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]})
            with self.subTest(raw=raw):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    planner = OpenAIPlanner(client, Settings(openai_api_key='test-only'))
                    planner.youtube_music._search = AsyncMock(return_value=[])
                    with self.assertRaises(MusicRecommendationFailed):
                        await planner.recommend_music(request())
                self.assertEqual(len(calls), 2)

    async def test_search_metadata_only_and_bad_output(self):
        async with httpx.AsyncClient() as client:
            for output, code in ((b'{"entries": []}', 0), (b'bad json', 0), (b'{}', 0), (b'', 1)):
                process = Mock(returncode=code, communicate=AsyncMock(return_value=(output, None)))
                with patch('ai_service.music.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
                    if output == b'{"entries": []}':
                        self.assertEqual(await YouTubeMusic(client)._search('Coldplay Yellow'), [])
                    else:
                        with self.assertRaises(ServiceUnavailable):
                            await YouTubeMusic(client)._search('Coldplay Yellow')
                    args = spawn.call_args.args
                    for flag in ('--skip-download', '--ignore-config', '--flat-playlist'):
                        self.assertIn(flag, args)
                    self.assertEqual(args[-1], 'ytsearch5:Coldplay Yellow')

    async def test_cancel_or_timeout_kills_search_process(self):
        async with httpx.AsyncClient() as client:
            for error in (asyncio.CancelledError(), TimeoutError()):
                process = Mock(returncode=None, communicate=AsyncMock(side_effect=error), wait=AsyncMock())
                with patch('ai_service.music.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
                    expected = asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else ServiceUnavailable
                    with self.assertRaises(expected):
                        await YouTubeMusic(client)._search('Coldplay Yellow')
                process.kill.assert_called_once()
                process.wait.assert_awaited_once()


class MusicContractTests(unittest.TestCase):
    def test_stream_request_has_only_existing_itinerary_fields(self):
        fields = ItineraryStreamRequest.model_json_schema()['properties']
        self.assertEqual(set(fields), set(ItineraryRequest.model_json_schema()['properties']))
        data = request().model_dump(mode='json')
        self.assertEqual(ItineraryStreamRequest.model_validate(data).itinerary_request(), request())
        with TestClient(create_app(settings=Settings(api_token='test-only'))) as client:
            schema = client.get('/openapi.json').json()
            self.assertNotIn('music_candidates', schema['components']['schemas']['TravelGenerationRequest']['properties'])
            self.assertNotIn('youtube.com/results', json.dumps(schema))
            data['music_candidates'] = []
            response = client.post('/api/ai/v1/itinerary-jobs/stream', json=data,
                                   headers={'Authorization': 'Bearer test-only'})
            self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
