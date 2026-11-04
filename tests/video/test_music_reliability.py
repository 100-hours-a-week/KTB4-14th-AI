import asyncio
from uuid import uuid4

import httpx
import numpy as np
import pytest

from ai_service.clients.openai import OpenAIClient
from ai_service.core.config import Settings
from ai_service.core.errors import ServiceUnavailable
from ai_service.services.video.errors import VideoError
from ai_service.services.video.score import SAMPLE_RATE, select_score_excerpt
from scripts.video.worker import job_seed, music_plans


def song(pause_at=10.0, pause_seconds=1.1, length=40.0, active_until=33.0):
    """Loud stereo tone with one silent pause, a 2 s fade-out, then silence, like an ACE take."""
    t = np.arange(round(length * SAMPLE_RATE)) / SAMPLE_RATE
    level = np.where(t < active_until, 0.3, 0.0)
    level = np.where((t > active_until - 2) & (t < active_until), 0.3 * (active_until - t) / 2, level)
    level = np.where((t >= pause_at) & (t < pause_at + pause_seconds), 0.0, level)
    wave = (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    return np.stack([wave, wave], axis=1)


def test_short_breath_before_the_main_section_is_accepted():
    excerpt = select_score_excerpt(song(pause_seconds=1.1), 30)
    assert excerpt['duration_seconds'] == 30
    from ai_service.services.video.score import ScorePolicy
    with pytest.raises(VideoError):  # the old 0.5 s limit rejected this same take
        select_score_excerpt(song(pause_seconds=1.1), 30, policy=ScorePolicy(maximum_internal_quiet_seconds=0.5))


def test_long_silence_is_still_rejected():
    with pytest.raises(VideoError, match='score_no_usable_continuous_excerpt'):
        select_score_excerpt(song(pause_seconds=4.0), 30)


def test_each_job_gets_its_own_music_seed():
    first, second = uuid4(), uuid4()
    assert job_seed(first) == job_seed(first) != job_seed(second)
    plans = music_plans('AUTO', first)
    assert plans[0][0] != plans[1][0] and plans[0][0] < 2**63
    assert music_plans('NONE', first) == [(job_seed(first), 'NONE', None)]


OK_BODY = {'choices': [{'message': {'content': '{"ok": true}'}, 'finish_reason': 'stop'}]}


def openai(responses, retries):
    queue = list(responses)
    calls = []
    def handle(request):
        calls.append(1)
        status = queue.pop(0)
        return httpx.Response(status, json=OK_BODY if status == 200 else {'error': 'x'})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    settings = Settings(openai_api_key='k', openai_model='m', model_timeout_seconds=5)
    return OpenAIClient(http, settings, retries=retries, retry_delays=(0, 0)), calls


def test_video_openai_calls_retry_brief_errors():
    client, calls = openai([429, 503, 200], retries=2)
    assert asyncio.run(client.complete([], {}, 'x')) == '{"ok": true}' and len(calls) == 3


def test_permanent_errors_and_default_clients_do_not_retry():
    client, calls = openai([400, 200], retries=2)
    with pytest.raises(ServiceUnavailable):
        asyncio.run(client.complete([], {}, 'x'))
    assert len(calls) == 1
    client, calls = openai([429, 200], retries=0)
    with pytest.raises(ServiceUnavailable):
        asyncio.run(client.complete([], {}, 'x'))
    assert len(calls) == 1
