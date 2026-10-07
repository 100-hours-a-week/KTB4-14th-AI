import json
import unittest

from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.main import create_app


class MatchingOpenAPITests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings=Settings())
        with TestClient(self.app) as client:
            self.spec = client.get("/openapi.json").json()
        self.operation = self.spec["paths"]["/matching-requests"]["post"]
        self.schemas = self.spec["components"]["schemas"]

    def test_request_uses_shared_auth_and_only_three_required_selections(self):
        self.assertEqual(self.operation["tags"], ["V2"])
        self.assertEqual(self.operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(set(self.spec["components"]["securitySchemes"]), {"HTTPBearer"})
        security = self.spec["components"]["securitySchemes"]["HTTPBearer"]
        self.assertEqual((security["type"], security["scheme"]), ("http", "bearer"))
        self.assertFalse(self.operation.get("parameters"))
        request = self.schemas["MatchingRequestCreate"]
        self.assertEqual(set(request["required"]), {"preferred_companion_gender", "theme", "pace"})
        self.assertEqual(set(request["properties"]), {
            "preferred_companion_gender", "theme", "pace", "budget_min", "budget_max",
        })
        self.assertEqual(request["properties"]["theme"]["items"]["type"], "string")
        for field in ("preferred_companion_gender", "pace"):
            self.assertEqual(request["properties"][field]["type"], "string")
        for field in ("budget_min", "budget_max"):
            self.assertEqual(request["properties"][field]["type"], "integer")
        self.assertTrue(self.schemas["MatchingRequestData"]["properties"]["user_id"]["readOnly"])

    def test_success_echoes_corrected_budget_example(self):
        request = self.operation["requestBody"]["content"]["application/json"]["examples"]["matching"]["value"]
        self.assertEqual(request, {
            "preferred_companion_gender": "Female", "theme": ["nature", "food"],
            "pace": "Balanced", "budget_min": 100000, "budget_max": 3000000,
        })
        response = self.operation["responses"]["201"]["content"]["application/json"]["examples"]["created"]["value"]
        self.assertEqual(response, {"message": "requests_success", "data": {"user_id": 1, **request}})
        self.assertNotIn("buget_max", json.dumps(self.operation))

    def test_all_documented_errors_preserve_null_data(self):
        responses = self.operation["responses"]
        self.assertEqual(set(responses), {"201", "400", "401", "500"})
        for code, messages in (
            ("400", {"preferred_companion_gender_required", "theme_required", "pace_required"}),
            ("401", {"unauthorized"}),
            ("500", {"Internal_server_error"}),
        ):
            values = [example["value"] for example in responses[code]["content"]["application/json"]["examples"].values()]
            self.assertEqual({value["message"] for value in values}, messages)
            for value in values:
                self.assertEqual(set(value), {"message", "data"})
                self.assertIsNone(value["data"])

    def test_planned_contract_is_visible_without_registering_execution_route(self):
        self.assertEqual(self.operation["x-implementation-status"], "planned")
        self.assertNotIn("description", self.operation)
        self.assertNotIn("/matching-requests", {route.path for route in self.app.routes})
        self.assertEqual(sum(tag["name"] == "V2" for tag in self.app.openapi()["tags"]), 1)


if __name__ == "__main__":
    unittest.main()
