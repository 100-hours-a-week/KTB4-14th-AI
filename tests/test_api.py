from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.jobs import LocalJobBackend
from ai_service.main import create_app


TOKEN = "test-token-that-is-long-enough-123456"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_client() -> TestClient:
    app = create_app(
        settings=Settings(
            api_token=TOKEN,
            mode="demo",
            gemini_api_key=None,
            gemini_itinerary_model="gemini-3.5-flash-lite",
        ),
        jobs=LocalJobBackend(synchronous=True),
    )
    return TestClient(app)


def itinerary() -> dict:
    return {
        "trip": {
            "title": "부산 바다 여행",
            "destination": {"provider_place_id": "city_busan", "name": "부산"},
            "start_date": "2026-10-10",
            "end_date": "2026-10-11",
            "daily_start_time": "10:00:00",
            "daily_end_time": "20:00:00",
            "travelers": 2,
            "companion_type": "FRIENDS",
        },
        "preferences": {
            "themes": ["자연", "미식"],
            "pace": "BALANCED",
            "transport_modes": ["PUBLIC_TRANSIT"],
            "budget_min_krw": 300000,
            "budget_max_krw": 800000,
            "distance_preference": 0.35,
            "cuisine_types": ["한식"],
        },
        "constraints": {
            "must_visit_place_ids": ["place_haeundae"],
            "excluded_place_ids": [],
        },
    }


def test_health_is_public():
    response = make_client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "demo"}


def test_protected_endpoint_requires_bearer_token():
    response = make_client().get("/api/ai/v1/jobs/unknown")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_itinerary_job_and_result_follow_sheet_contract():
    client = make_client()
    response = client.post(
        "/api/ai/v1/itinerary-jobs",
        json=itinerary(),
        headers={**AUTH, "Idempotency-Key": "request-key-0001"},
    )
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["job_type"] == "ITINERARY_GENERATION"
    result = client.get(accepted["status_url"], headers=AUTH)
    assert result.status_code == 200
    assert result.json()["status"] == "SUCCEEDED"
    assert result.json()["result"]["days"][0]["date"] == "2026-10-10"


def test_itinerary_idempotency_and_conflict():
    client = make_client()
    headers = {**AUTH, "Idempotency-Key": "request-key-0002"}
    first = client.post("/api/ai/v1/itinerary-jobs", json=itinerary(), headers=headers)
    second = client.post("/api/ai/v1/itinerary-jobs", json=itinerary(), headers=headers)
    assert first.json()["job_id"] == second.json()["job_id"]
    changed = itinerary()
    changed["trip"]["title"] = "다른 요청"
    conflict = client.post("/api/ai/v1/itinerary-jobs", json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_invalid_body_uses_common_error_schema():
    body = itinerary()
    body["trip"]["end_date"] = "2026-10-01"
    response = make_client().post(
        "/api/ai/v1/itinerary-jobs",
        json=body,
        headers={**AUTH, "Idempotency-Key": "request-key-0003"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.json()["request_id"].startswith("req_")


def test_music_recommendation():
    response = make_client().post(
        "/api/ai/v1/music-recommendations",
        headers=AUTH,
        json={
            "trip_context": {
                "destination": "부산",
                "season": "AUTUMN",
                "mood_tags": ["청량한", "드라이브"],
            },
            "music_preferences": {"genres": ["INDIE", "POP"], "excluded_track_ids": []},
        },
    )
    assert response.status_code == 200
    assert response.json()["track"]["provider_track_id"] == "demo_track_001"


def test_matching_job():
    client = make_client()
    response = client.post(
        "/api/ai/v1/traveler-match-jobs",
        headers=AUTH,
        json={
            "matching_request_id": 201,
            "travel_plan_id": 101,
            "requester_id": 12,
            "preferred_companion_gender": "ANY",
            "pace": "BALANCED",
            "themes": ["자연", "미식"],
            "candidates": [
                {"user_id": 35, "pace": "BALANCED", "themes": ["자연", "사진"]}
            ],
            "limit": 3,
        },
    )
    assert response.status_code == 202
    result = client.get(response.json()["status_url"], headers=AUTH).json()
    assert result["result"]["matches"][0]["user_id"] == 35


def test_checklist():
    response = make_client().post(
        "/api/ai/v1/checklists",
        headers=AUTH,
        json={
            "travel_plan_id": 101,
            "trip_context": {
                "destination": "부산",
                "start_date": "2026-10-10",
                "end_date": "2026-10-11",
                "travelers": 2,
                "themes": ["자연", "미식"],
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["sort_order"] == 1


def test_video_job_reports_unconfigured_feature():
    client = make_client()
    response = client.post(
        "/api/ai/v1/video-jobs",
        headers=AUTH,
        json={
            "travel_plan_id": 101,
            "title": "부산 여행 하이라이트",
            "media_urls": ["https://example.com/photo-01.jpg"],
            "style": "HIGHLIGHT",
            "duration_seconds": 30,
        },
    )
    result = client.get(response.json()["status_url"], headers=AUTH).json()
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "FEATURE_NOT_CONFIGURED"


def test_unknown_job_returns_sheet_error():
    response = make_client().get("/api/ai/v1/jobs/unknown", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_openapi_contains_all_sheet_endpoints():
    paths = make_client().get("/openapi.json").json()["paths"]
    assert {
        "/api/ai/v1/itinerary-jobs",
        "/api/ai/v1/jobs/{job_id}",
        "/api/ai/v1/music-recommendations",
        "/api/ai/v1/traveler-match-jobs",
        "/api/ai/v1/checklists",
        "/api/ai/v1/video-jobs",
    }.issubset(paths)
