import asyncio

import pytest

from ai_service.services.video.film import run_lanes


def test_two_lanes_share_the_queue_and_a_booting_second_gpu_never_delays_the_film():
    queue, done = list(range(6)), []

    async def lane(name, state, delay):
        while queue:
            slot = queue.pop(0)
            state['busy'] = True
            await asyncio.sleep(delay)
            done.append((name, slot))
            state['busy'] = False

    async def booting_forever(state):
        await asyncio.sleep(3600)  # a second GPU that never becomes ready

    async def scenario():
        await asyncio.wait_for(run_lanes(lambda s: lane('a', s, 0.01), [booting_forever]), 2)

    asyncio.run(scenario())
    assert sorted(slot for _, slot in done) == list(range(6))


def test_work_is_split_between_two_ready_gpus():
    queue, done = list(range(6)), []

    async def lane(name, state):
        while queue:
            slot = queue.pop(0)
            state['busy'] = True
            await asyncio.sleep(0.01)
            done.append((name, slot))
            state['busy'] = False

    asyncio.run(run_lanes(lambda s: lane('a', s), [lambda s: lane('b', s)]))
    assert {name for name, _ in done} == {'a', 'b'} and len(done) == 6


def test_a_failing_lane_stops_the_others():
    async def ok(state):
        await asyncio.sleep(0.05)

    async def broken(state):
        raise RuntimeError('scene failed')

    with pytest.raises(RuntimeError):
        asyncio.run(run_lanes(broken, [ok]))
