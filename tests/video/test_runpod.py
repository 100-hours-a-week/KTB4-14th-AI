"""RunPod 탐색 조건·인증 분리·주소 캐시·Pod 교체 시 접속 주소 갱신을 검사한다."""
from dataclasses import replace
import unittest

import httpx

from ai_service.clients.runpod import EndpointResolver
from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError


def pod(identifier, *, name="audigo-video-test", status="RUNNING", ports=None):
    return {"id": identifier, "name": name, "desiredStatus": status, "ports": ports if ports is not None else ["8188/http"]}


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **values):
        return replace(VideoSettings(), endpoint_mode="runpod", runpod_api_key="private-runpod-key",
                       comfyui_token="private-comfy-key", **values)

    async def test_filters_checks_readiness_caches_and_does_not_leak_account_key(self):
        lists = []
        def handle(request):
            if request.url.host == "rest.runpod.io":
                lists.append(request)
                self.assertEqual(request.headers["Authorization"], "Bearer private-runpod-key")
                self.assertEqual(request.url.path, "/v1/pods")
                return httpx.Response(200, json=[pod("stopped", status="EXITED"), pod("other", name="other"), pod("closed", ports=["8188/tcp"]), pod("a"), pod("b")])
            self.assertEqual(request.headers["Authorization"], "Bearer private-comfy-key")
            self.assertNotIn("private-runpod-key", str(request.headers))
            return httpx.Response(503) if request.url.host.startswith("a-") else httpx.Response(200, json={"system": {}, "devices": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            resolver = EndpointResolver(http, self.settings())
            first = await resolver.resolve()
            second = await resolver.resolve()
            self.assertEqual(first.pod_id, "b")
            self.assertEqual(second, first)
            self.assertEqual(len(lists), 1)

    async def test_replacement_pod_is_found_after_cached_health_fails(self):
        current, lists = ["old"], []
        def handle(request):
            if request.url.host == "rest.runpod.io":
                lists.append(1)
                return httpx.Response(200, json=[pod(current[0])])
            if request.url.host != f"{current[0]}-8188.proxy.runpod.net":
                return httpx.Response(502)
            return httpx.Response(200, json={"system": {}, "devices": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            resolver = EndpointResolver(http, self.settings())
            first = await resolver.resolve()
            current[0] = "new"
            second = await resolver.resolve()
            self.assertEqual((first.pod_id, second.pod_id), ("old", "new"))
            self.assertEqual(len(lists), 2)

    async def test_expired_cache_api_outage_no_candidates_and_pin(self):
        for response, reason in ((httpx.Response(503), "runpod_discovery_unavailable"),
                                 (httpx.Response(200, json={}), "runpod_response_invalid"),
                                 (httpx.Response(200, json=[pod("other")]), "runpod_no_ready_pod")):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
                resolver = EndpointResolver(http, self.settings(discovery_ttl=0, runpod_pod_id="only"))
                with self.assertRaises(VideoError) as error:
                    await resolver.resolve()
                self.assertEqual(error.exception.reason, reason)

    async def test_missing_account_key_and_static_url(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"system": {}, "devices": []}))) as http:
            resolver = EndpointResolver(http, replace(self.settings(), runpod_api_key=None))
            with self.assertRaises(VideoError):
                await resolver.resolve()
            endpoint = await EndpointResolver(http, VideoSettings(comfyui_url="http://localhost:8188/")).resolve()
            self.assertEqual(endpoint.url, "http://localhost:8188")
