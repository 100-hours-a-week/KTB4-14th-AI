"""Test the SSE boundary without changing or re-running generation logic."""
import json
import unittest
from unittest.mock import Mock, patch

from ai_service.config import Settings
from ai_service.streaming import stream_generation
from test_day_transport import request


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_final_result_is_serialized_once(self):
        intermediate = Mock()
        intermediate.model_dump.side_effect = AssertionError("internal result must not be serialized")
        final = Mock()
        final.model_dump.return_value = {"itinerary": {"marker": "complete itinerary"}, "music": {"marker": "complete song"}}

        async def pipeline(*args):
            for stage in ("PLACES", "ACCOMMODATIONS", "ROUTES", "MUSIC"):
                yield stage, "STARTED", None
                yield stage, "COMPLETED", intermediate
            yield "COMPLETE", "COMPLETED", final

        with patch("ai_service.streaming.generation_stages", side_effect=pipeline):
            chunks = [chunk async for chunk in stream_generation(request(), None, None, Settings(), "req_test")]
        self.assertEqual(len(chunks), 9)
        for index, chunk in enumerate(chunks, 1):
            self.assertTrue(chunk.startswith(f"id: {index}\n"))
        payloads = [json.loads(chunk.split("data: ", 1)[1]) for chunk in chunks]
        self.assertTrue(all(set(p) == {"stage", "status"} for p in payloads[:-1]))
        self.assertEqual(payloads[-1], {
            "generation_job_id": 1, "stage": "COMPLETE", "status": "COMPLETED",
            "data": final.model_dump.return_value,
        })
        intermediate.model_dump.assert_not_called()
        final.model_dump.assert_called_once_with(mode="json")


if __name__ == "__main__":
    unittest.main()
