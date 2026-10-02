"""Night visit boundaries apply to planning, both schedulers and HTTP output."""
from datetime import datetime
import unittest
from unittest.mock import Mock

import httpx
from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.errors import GenerationFailed, InvalidModelOutput
from ai_service.features import day_windows, schedule_selection, validate_itinerary
from ai_service.main import create_app
from ai_service.pipeline import generate_plan
from ai_service.routing import KakaoRoutes, schedule_with_routes
from ai_service.schemas import KST, ModelSelection
from test_compact_routes import PlaceClient, Planner, all_keys
from test_day_transport import request, places


def night_request(extra=None, arrival=18, departure=23):
    body = request(extra, 'CAR')
    body.duration.arrival_datetime = datetime(2026, 9, 19, arrival, tzinfo=KST)
    body.duration.departure_datetime = datetime(2026, 9, 19, departure, 59, tzinfo=KST)
    return body


def night_selection():
    return ModelSelection(title='밤 여행', days=[{
        'date': '2026-09-19', 'items': [
            {'provider_place_id': '0'}, {'provider_place_id': '1'},
        ],
    }])


class NightScheduleTests(unittest.IsolatedAsyncioTestCase):
    def test_evening_arrival_extends_planning_to_2359_and_keeps_buffer(self):
        for hour, start in ((18, '18:45'), (21, '21:45'), (23, '23:45')):
            body = night_request(arrival=hour)
            for schedule in (False, True):
                with self.subTest(hour=hour, schedule=schedule):
                    window = day_windows(body, schedule=schedule)[0]
                    self.assertEqual((window['start'], window['end']), (start, '23:59'))

    def test_night_only_applies_to_every_date_and_leaves_dawn_day_empty(self):
        for text in ('밤 일정만 만들어주세요', '야간 일정만', '저녁 시간대에만 여행'):
            body = night_request(text, arrival=10)
            body.duration.departure_datetime = datetime(2026, 9, 21, 5, tzinfo=KST)
            for schedule in (False, True):
                windows = day_windows(body, schedule=schedule)
                self.assertEqual([(w['start'], w['end']) for w in windows[:2]],
                                 [('18:00', '23:59'), ('18:00', '23:59')])
                self.assertEqual(windows[-1]['available_minutes'], 0)
                self.assertEqual(windows[-1]['max_items'], 0)

    def test_evening_arrival_keeps_following_day_normal(self):
        body = night_request()
        body.duration.departure_datetime = datetime(2026, 9, 20, 18, tzinfo=KST)
        self.assertEqual(day_windows(body)[1]['start'], '09:00')
        body = request('밤에는 숙소에서 쉬고 싶어요', 'CAR')
        self.assertEqual(day_windows(body)[0]['end'], '21:00')

    def test_night_boundaries_use_korean_local_time(self):
        body = night_request()
        from ai_service.schemas import Duration
        body.duration = Duration(arrival_datetime='2026-09-19T09:00:00Z',
                                 departure_datetime='2026-09-19T14:59:00Z')
        self.assertEqual(day_windows(body)[0]['start'], '18:45')
        self.assertEqual(day_windows(body)[0]['end'], '23:59')

    async def test_both_schedulers_stop_visits_before_midnight(self):
        body, pool, selected = night_request(), places(), night_selection()
        estimated = validate_itinerary(body, schedule_selection(body, selected, pool), pool)
        async with httpx.AsyncClient() as client:
            routed = (await schedule_with_routes(body, selected, pool, 'test',
                      KakaoRoutes(client, Settings()))).itinerary.days
        for days in (estimated, routed):
            for item in days[0].items:
                self.assertGreaterEqual(item.start_time, '18:45')
                self.assertLessEqual(item.end_time, '23:59')
                self.assertLess(item.start_time, item.end_time)

    def test_validation_rejects_visit_crossing_midnight(self):
        body, pool = night_request(), places()
        generated = schedule_selection(body, night_selection(), pool)
        generated.days[0].items[-1].start_time = '23:30'
        generated.days[0].items[-1].stay_minutes = 60
        with self.assertRaises(InvalidModelOutput):
            validate_itinerary(body, generated, pool)

    async def test_dawn_only_night_request_fails_before_external_calls(self):
        body = night_request('밤 일정만', arrival=1, departure=5)
        collector, planner = Mock(), Mock()
        with self.assertLogs('uvicorn.error.audigo', level='ERROR'), self.assertRaises(GenerationFailed):
            await generate_plan(body, collector, planner)
        collector.collect.assert_not_called()
        planner.generate.assert_not_called()

    def test_http_night_response_preserves_compact_contract(self):
        class NightPlanner(Planner):
            async def generate(self, context, feedback, places_only=False):
                return night_selection()

        settings = Settings(api_token='test', openai_api_key='test', kakao_rest_api_key='test')
        app = create_app(settings=settings)
        payload = night_request().model_dump(mode='json')
        payload.pop('travel_plan_id', None)
        with TestClient(app) as client:
            app.state.places = PlaceClient()
            app.state.planner = NightPlanner()
            response = client.post('/internal/ai/itineraries/generate',
                json=payload,
                headers={'Authorization': 'Bearer test'})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        for item in result['days'][0]['items']:
            self.assertGreaterEqual(item['start_time'], '18:45')
            self.assertLessEqual(item['end_time'], '23:59')
        route = result['days'][0]['items'][1]['route_from_previous']
        self.assertEqual(set(route), {'transport_type', 'duration_minutes', 'distance_meter',
                                     'line_name', 'vehicle_number', 'total_fare_amount', 'legs'})
        self.assertFalse(all_keys(result) & {'boarding_stop', 'alighting_stop', 'path'})
