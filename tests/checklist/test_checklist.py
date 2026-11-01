"""실제 라우터·인증·모델 HTTP 어댑터·출력 검증을 함께 검사한다."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_service.api.examples.checklist import CHECKLIST_REQUEST_EXAMPLE, CHECKLIST_RESPONSE_EXAMPLE
from ai_service.core.config import Settings
from ai_service.main import create_app


PATH = '/api/ai/v1/checklists'
HEADERS = {'Authorization': 'Bearer test-only'}
SETTINGS = Settings(api_token='test-only', openai_api_key='test-model-key')
GENERATED = {
    'title': CHECKLIST_RESPONSE_EXAMPLE['title'],
    'items': [{'content': item['content']} for item in CHECKLIST_RESPONSE_EXAMPLE['items']],
}


def completion(raw=None, *, refusal=None, finish='stop'):
    return httpx.Response(200, json={'choices': [{
        'finish_reason': finish,
        'message': {'content': json.dumps(GENERATED, ensure_ascii=False) if raw is None else raw,
                    'refusal': refusal},
    }]})


@pytest.fixture
def api():
    calls = []
    replies = []

    def handle(request):
        assert str(request.url) == 'https://api.openai.com/v1/chat/completions'
        assert request.method == 'POST'
        calls.append(json.loads(request.content))
        reply = replies.pop(0) if replies else completion()
        if isinstance(reply, Exception):
            raise reply
        return reply

    app = create_app(settings=SETTINGS, transport=httpx.MockTransport(handle))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, calls, replies, app


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {'error', 'request_id'}
    assert body['request_id'].startswith('req_')
    assert response.headers['X-Request-Id'] == body['request_id']
    assert body['error']['code'] == code
    assert body['error']['retryable'] is (status == 503)
    assert isinstance(body['error']['details'], list)
    return body


def test_success_uses_trip_context_and_server_owned_fields(api):
    client, calls, _, _ = api
    response = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == CHECKLIST_RESPONSE_EXAMPLE
    assert response.headers['X-Request-Id'].startswith('req_')
    assert len(calls) == 1
    call = calls[0]
    assert json.loads(call['messages'][1]['content']) == CHECKLIST_REQUEST_EXAMPLE['trip_context']
    assert call['model'] == SETTINGS.openai_model
    schema = call['response_format']['json_schema']
    assert schema['strict'] is True
    assert schema['schema']['additionalProperties'] is False
    assert set(schema['schema']['required']) == {'title', 'items'}
    assert set(schema['schema']['$defs']['GeneratedChecklistItem']['required']) == {'content'}


@pytest.mark.parametrize('field,value', [
    ('travel_plan_id', 0), ('travel_plan_id', -1), ('travel_plan_id', True),
    ('travel_plan_id', '101'), ('travel_plan_id', 1.0), ('travel_plan_id', None),
    ('destination', ''), ('destination', '   '), ('destination', None), ('destination', 123),
    ('destination', 'x' * 501),
    ('travelers', 0), ('travelers', -1), ('travelers', True), ('travelers', '2'),
    ('travelers', 2.0), ('travelers', None),
    ('start_date', '2026-02-30'), ('start_date', '20261110'),
    ('start_date', '2026-11-10T00:00:00'), ('start_date', 1794268800),
    ('end_date', '2026-11-09'), ('end_date', '2026-11-31'),
    ('themes', []), ('themes', ['NATURE', 'NATURE']), ('themes', ['UNKNOWN']),
    ('themes', ['nature']), ('themes', ['NATURE', 'FOOD', 'REST', 'SNS']),
    ('themes', None), ('themes', 'NATURE'),
])
def test_invalid_fields_are_400_before_model_call(api, field, value):
    client, calls, _, _ = api
    body = deepcopy(CHECKLIST_REQUEST_EXAMPLE)
    target = body if field == 'travel_plan_id' else body['trip_context']
    target[field] = value
    result = assert_error(client.post(PATH, json=body, headers=HEADERS), 400, 'INVALID_REQUEST')
    assert field in result['error']['details'][0]['field'].split('.')
    assert calls == []


@pytest.mark.parametrize('field', ['travel_plan_id', 'trip_context', 'destination', 'start_date',
                                   'end_date', 'travelers', 'themes'])
def test_all_fields_are_required(api, field):
    client, calls, _, _ = api
    body = deepcopy(CHECKLIST_REQUEST_EXAMPLE)
    target = body if field in ('travel_plan_id', 'trip_context') else body['trip_context']
    target.pop(field)
    result = assert_error(client.post(PATH, json=body, headers=HEADERS), 400, 'INVALID_REQUEST')
    assert result['error']['details'][0]['type'] == 'missing'
    assert calls == []


@pytest.mark.parametrize('body', [
    b'{"travel_plan_id":', b'', b'null', b'[]',
    json.dumps({**CHECKLIST_REQUEST_EXAMPLE, 'user_id': 1}).encode(),
    json.dumps({**CHECKLIST_REQUEST_EXAMPLE, 'trip_context': {
        **CHECKLIST_REQUEST_EXAMPLE['trip_context'], 'ignored': True}}).encode(),
])
def test_malformed_and_extra_fields(api, body):
    client, calls, _, _ = api
    assert_error(client.post(PATH, content=body,
                            headers={**HEADERS, 'Content-Type': 'application/json'}), 400, 'INVALID_REQUEST')
    assert calls == []


def test_same_day_trip_and_whitespace_trim(api):
    client, _, _, _ = api
    body = deepcopy(CHECKLIST_REQUEST_EXAMPLE)
    body['trip_context'].update(end_date='2026-11-10', destination='  부산  ')
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 200


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer wrong'},
                                      {'Authorization': 'Basic test-only'}])
def test_authentication(api, headers):
    client, calls, _, _ = api
    response = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=headers)
    assert_error(response, 401, 'UNAUTHORIZED')
    assert response.headers['www-authenticate'] == 'Bearer'
    assert calls == []


@pytest.mark.parametrize('settings', [Settings(), Settings(api_token='test-only')])
def test_missing_configuration_is_503(settings):
    def unexpected_call(request):
        pytest.fail('missing configuration must not call external services')
    with TestClient(create_app(settings=settings, transport=httpx.MockTransport(unexpected_call))) as client:
        result = assert_error(client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS),
                              503, 'AI_SERVICE_UNAVAILABLE')
    assert result['error']['message'] == '잠시 후 다시 시도해 주세요.'


@pytest.mark.parametrize('raw', [
    'not json', '{}', 'null',
    json.dumps({**GENERATED, 'title': '   '}),
    json.dumps({**GENERATED, 'title': 'x' * 101}),
    json.dumps({**GENERATED, 'items': []}),
    json.dumps({**GENERATED, 'items': [{'content': '  '}]}),
    json.dumps({**GENERATED, 'items': [{'content': 'x' * 201}]}),
    json.dumps({**GENERATED, 'items': [{'content': str(i)} for i in range(51)]}),
    json.dumps({**GENERATED, 'items': [{'content': '충전기'}, {'content': ' 충전기 '}]}),
    json.dumps({**GENERATED, 'items': [{'content': 'ID  card'}, {'content': 'id card'}]}),
    json.dumps({**GENERATED, 'travel_plan_id': 999}),
    json.dumps({**GENERATED, 'items': [{'content': '충전기', 'is_checked': True}]}),
])
def test_invalid_model_outputs_retry_then_422(api, raw):
    client, calls, replies, _ = api
    replies.extend([completion(raw), completion(raw)])
    result = assert_error(client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS),
                          422, 'CHECKLIST_GENERATION_FAILED')
    assert result['error']['message'] == '체크리스트를 생성하지 못했습니다.'
    assert len(calls) == 2


def test_invalid_output_recovers_on_second_attempt(api):
    client, calls, replies, _ = api
    replies.extend([completion('{}'), completion()])
    assert client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS).json() == CHECKLIST_RESPONSE_EXAMPLE
    assert len(calls) == 2
    assert len(calls[1]['messages']) == 3


def test_refusal_is_checklist_422_without_retry(api):
    client, calls, replies, _ = api
    replies.append(completion(refusal='private provider refusal'))
    response = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS)
    assert_error(response, 422, 'CHECKLIST_GENERATION_FAILED')
    assert 'private provider refusal' not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize('reply', [
    httpx.Response(429, text='private provider body'),
    httpx.Response(500, text='private provider body'),
    httpx.ConnectError('private transport error'),
    httpx.ReadTimeout('private transport error'),
])
def test_provider_failures_are_503_without_generation_retry(api, reply):
    client, calls, replies, _ = api
    replies.append(reply)
    response = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS)
    assert_error(response, 503, 'AI_SERVICE_UNAVAILABLE')
    assert 'private' not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize('reply', [
    completion(finish='length'), httpx.Response(200, json={}),
    httpx.Response(200, text='bad json'), httpx.Response(200, json={'choices': []}),
])
def test_incomplete_model_envelopes_retry_then_fail(api, reply):
    client, calls, replies, _ = api
    replies.extend([reply, reply])
    assert_error(client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS),
                 422, 'CHECKLIST_GENERATION_FAILED')
    assert len(calls) == 2


def test_total_deadline_cancels_model_request():
    cancelled = []

    async def slow_model(request):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return completion()

    app = create_app(settings=replace(SETTINGS, generation_timeout_seconds=0.02),
                     transport=httpx.MockTransport(slow_model))
    with TestClient(app) as client:
        assert_error(client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS),
                     503, 'AI_SERVICE_UNAVAILABLE')
    assert cancelled == [True]


def test_unexpected_failure_is_sanitized_and_has_request_id(api, caplog):
    client, _, _, app = api
    app.state.checklist_generator.generate = AsyncMock(side_effect=RuntimeError('private secret'))
    response = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS)
    assert_error(response, 500, 'INTERNAL_SERVER_ERROR')
    assert 'private secret' not in response.text
    assert 'private secret' not in caplog.text


def test_swagger_examples_match_runtime_errors(api):
    client, _, replies, _ = api
    operation = client.get('/openapi.json').json()['paths'][PATH]['post']
    assert operation['tags'] == ['V3']
    assert operation['security'] == [{'HTTPBearer': []}]
    assert set(operation['responses']) == {'200', '400', '401', '422', '500', '503'}
    bad = deepcopy(CHECKLIST_REQUEST_EXAMPLE)
    bad['trip_context'].pop('destination')
    responses = {
        400: client.post(PATH, json=bad, headers=HEADERS),
        401: client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE),
    }
    replies.append(completion(refusal='refused'))
    responses[422] = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS)
    replies.append(httpx.Response(503))
    responses[503] = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS)
    for status, response in responses.items():
        assert response.status_code == status
        actual = response.json()
        actual['request_id'] = 'req_checklist_example'
        assert actual == operation['responses'][str(status)]['content']['application/json']['example']


def test_repeated_requests_return_fresh_initial_state(api):
    client, calls, _, _ = api
    first = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS)
    second = client.post(PATH, json=CHECKLIST_REQUEST_EXAMPLE, headers=HEADERS)
    assert first.json() == second.json() == CHECKLIST_RESPONSE_EXAMPLE
    assert first.headers['X-Request-Id'] != second.headers['X-Request-Id']
    assert len(calls) == 2
