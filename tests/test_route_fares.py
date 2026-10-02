"""Transit wire contract, provider fares and ordered walk/transfer regressions."""
import json
import math
import unittest
from datetime import datetime

import httpx
from fastapi.testclient import TestClient

from ai_service.api_examples import ROUTE_RESPONSE_EXAMPLE
from ai_service.config import Settings
from ai_service.main import create_app
from ai_service.routing import KakaoRoutes, summarize_route
from ai_service.schemas import KST, RouteSummary
from test_day_transport import places, transit_payload
from test_compact_routes import all_keys


def mixed_payload(origin, destination):
    points = [[origin.longitude + (destination.longitude-origin.longitude)*i/3,
               origin.latitude + (destination.latitude-origin.latitude)*i/3] for i in range(4)]
    steps = []
    for i, (mode, seconds, distance, start, end) in enumerate([
        ('BUS', 601, 5000, '출발 정류장', '환승 정류장'),
        ('WALKING', 61, 100, '환승 정류장', '환승역'),
        ('SUBWAY', 300, 7000, '환승역', '도착역'),
    ]):
        steps.append({'properties': {
            'type': mode, 'time': seconds, 'distance': distance,
            'guidance': '환승역까지 걸어가기',
            'stops': [{'name': start}, {'name': end}],
            'vehicles': [] if mode == 'WALKING' else [{'name': '201' if mode == 'BUS' else '9호선 급행', 'type': '일반'}],
        }, 'path': {'points': points[i:i+2]}})
    return {'status': 'OK', 'routes': [{'properties': {
        'totalTime': 1000, 'totalDistance': 12100, 'fare': {'value': 1650},
    }, 'steps': steps}]}


class RouteFareTests(unittest.IsolatedAsyncioTestCase):
    async def route(self, payload, mode='PUBLIC_TRANSPORT'):
        origin, destination = places()[:2]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
            return await KakaoRoutes(client, Settings(kakao_rest_api_key='test')).route(
                origin, destination, datetime(2026, 9, 25, 10, tzinfo=KST), mode)

    async def test_transfer_walk_order_minutes_stops_and_fare(self):
        payload = mixed_payload(*places()[:2])
        details = await self.route(payload)
        result = summarize_route(details).model_dump(mode='json')
        self.assertEqual([l['mode'] for l in result['legs']], ['BUS', 'WALK', 'SUBWAY'])
        self.assertEqual([math.ceil(l.duration_seconds / 60) for l in details.legs], [11, 2, 5])
        self.assertEqual([l.distance_meter for l in details.legs], [5000, 100, 7000])
        self.assertEqual(result['duration_minutes'], math.ceil(1000/60))
        self.assertEqual(result['distance_meter'], 12100)
        self.assertEqual(details.total_fare_amount, 1650)
        self.assertEqual(summarize_route(details).total_fare_amount, 1650)
        for leg in result['legs']:
            self.assertEqual(set(leg), {'mode', 'line_name', 'vehicle_number', 'start', 'end'})
            self.assertEqual(set(leg['start']), {'name', 'station_number'})
            self.assertIsNone(leg['start']['station_number'])
            self.assertIsNone(leg['end']['station_number'])
        walk = result['legs'][1]
        self.assertEqual(walk['start']['name'], '환승 정류장')
        self.assertEqual(walk['end']['name'], '환승역')
        self.assertIsNone(walk['vehicle_number'])
        self.assertIsNone(walk['line_name'])
        self.assertEqual(result['legs'][2]['line_name'], '9호선 급행')
        self.assertEqual(result['vehicle_number'], '201')
        self.assertEqual(result['line_name'], '9호선 급행')
        self.assertFalse(all_keys(result) & {'fare_amount', 'sequence', 'duration_minute', 'path', 'latitude', 'longitude', 'station_id'})
        # Explicit verified enrichment is preserved; provider IDs never substitute.
        details.legs[2].start.station_number = '00123'
        details.legs[2].end.station_number = '00456'
        enriched = summarize_route(details)
        self.assertEqual(enriched.legs[2].start.station_number, '00123')
        self.assertEqual(enriched.legs[2].end.station_number, '00456')

    async def test_selected_route_fare_not_first_route_or_cheapest_fare(self):
        payload = transit_payload(*places()[:2])
        payload['routes'][0]['properties']['fare'] = {'value': 1650}
        payload['routes'][1]['properties']['fare'] = {'value': 1000}
        payload['routes'].reverse()  # Bus appears first; subway is faster but dearer.
        details = await self.route(payload)
        self.assertEqual(details.legs[0].mode, 'SUBWAY')
        self.assertEqual(details.total_fare_amount, 1650)
        self.assertEqual(summarize_route(details).total_fare_amount, 1650)
        self.assertEqual((await self.route(payload, 'BUS')).total_fare_amount, 1000)

    async def test_missing_range_or_invalid_fares_stay_null(self):
        for fare in (None, {}, {'min': 1000, 'max': 2000}, {'min': 1650, 'max': 1650},
                     {'value': None}, {'value': True}, {'value': -1}, {'value': '1650'},
                     {'value': 1650.5}, {'value': 1650.0}, [], '1650'):
            with self.subTest(fare=fare):
                payload = mixed_payload(*places()[:2])
                payload['routes'][0]['properties']['fare'] = fare
                # Never sum incidental/undocumented step fare values.
                for step in payload['routes'][0]['steps']:
                    step['properties']['fare'] = {'value': 1650}
                details = await self.route(payload)
                self.assertIsNone(details.total_fare_amount)
                self.assertIsNone(summarize_route(details).model_dump()['total_fare_amount'])
        payload = mixed_payload(*places()[:2])
        del payload['routes'][0]['properties']['fare']
        self.assertIsNone((await self.route(payload)).total_fare_amount)
        payload['routes'][0]['properties']['fare'] = {'value': 0}
        self.assertEqual((await self.route(payload)).total_fare_amount, 0)

    async def test_walk_and_fallback_zero_car_null(self):
        origin, destination = places()[:2]
        walk = {'status': 'OK', 'route': {'properties': {'totalTime': 61, 'totalDistance': 100},
                'legs': [{'steps': [{'properties': {'guidance': '걷기', 'distance': 100},
                'path': {'points': [[origin.longitude, origin.latitude], [destination.longitude, destination.latitude]]}}]}]}}
        for mode in ('WALK', 'PUBLIC_TRANSPORT', 'CAR'):
            def handle(req):
                return httpx.Response(200, json={'status': 'NO_RESULTS'} if req.url.path.endswith('publictraffic') else walk)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                details = await KakaoRoutes(client, Settings(kakao_rest_api_key='test')).route(origin, destination, datetime(2026,9,25,10), mode)
            result = summarize_route(details).model_dump(mode='json')
            if mode == 'CAR':
                self.assertIsNone(details.total_fare_amount)
                self.assertIsNone(result['total_fare_amount'])
                self.assertEqual(result['legs'], [])
            else:
                self.assertEqual(result['total_fare_amount'], 0)
                self.assertEqual(result['legs'][0]['mode'], 'WALK')
                self.assertEqual(math.ceil(details.legs[0].duration_seconds / 60), 2)
                self.assertEqual(result['legs'][0]['start']['name'], origin.place_name)
                self.assertEqual(result['legs'][0]['end']['name'], destination.place_name)


class RouteOpenApiTests(unittest.TestCase):
    def test_json_schema_example_matches_public_contract(self):
        with TestClient(create_app(settings=Settings())) as client:
            spec = client.get('/openapi.json').json()
        example = spec['components']['schemas']['RouteSummary']['examples'][0]
        self.assertEqual(example, ROUTE_RESPONSE_EXAMPLE)
        self.assertEqual(RouteSummary.model_validate(example).model_dump(mode='json'), example)
        self.assertNotIn('/api/ai/v1/itinerary-jobs/stream', spec['paths'])
        self.assertIsNone(example['legs'][0]['start']['station_number'])
