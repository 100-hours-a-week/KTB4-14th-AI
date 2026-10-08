import unittest

from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.main import create_app
from ai_service.matching import MatchingRequestCreate
from ai_service.matching_examples import MATCHING_CREATE_EXAMPLE, MATCHING_REQUEST_EXAMPLES, MATCHING_RESPONSE_EXAMPLES


class MatchingOpenAPITests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings=Settings())
        with TestClient(self.app) as client:
            self.spec = client.get("/openapi.json").json()
        self.operation = self.spec["paths"]["/matching-requests"]["post"]

    def test_agreed_path_is_registered_once_and_old_internal_path_is_absent(self):
        self.assertEqual(sum(route.path == "/matching-requests" for route in self.app.routes), 1)
        self.assertNotIn("/internal/ai/matching/recommend", self.spec["paths"])
        self.assertNotIn("x-implementation-status", self.operation)
        self.assertEqual(self.operation["operationId"], "create_matching_request_v2")
        self.assertEqual(self.operation["tags"], ["V2"])

    def test_single_bearer_scheme_is_used(self):
        self.assertEqual(self.operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(set(self.spec["components"]["securitySchemes"]), {"HTTPBearer"})

    def test_examples_and_schema_accept_only_the_fullstack_fields(self):
        request = self.operation["requestBody"]["content"]["application/json"]
        self.assertEqual(request["schema"]["$ref"], "#/components/schemas/MatchingRequestCreate")
        shown = request["examples"]["mock"]["value"]
        self.assertEqual(shown, MATCHING_CREATE_EXAMPLE)
        self.assertEqual(request["examples"], MATCHING_REQUEST_EXAMPLES)
        allowed = {"preferred_companion_gender", "theme", "pace", "budget_min", "budget_max"}
        for example in request["examples"].values():
            self.assertLessEqual(set(example["value"]), allowed)
            MatchingRequestCreate.model_validate(example["value"])
        self.assertEqual(shown["budget_max"], 3000000)
        schema = self.spec["components"]["schemas"]["MatchingRequestCreate"]
        self.assertEqual(set(schema["properties"]), allowed)
        self.assertEqual(set(schema["required"]), {"preferred_companion_gender", "theme", "pace"})
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("MatchingCandidate", self.spec["components"]["schemas"])
        errors = self.operation["responses"]["400"]["content"]["application/json"]["examples"]
        self.assertNotIn("candidates", errors)

    def test_response_examples_preserve_null_and_match_the_declared_status(self):
        for code, examples in MATCHING_RESPONSE_EXAMPLES.items():
            shown = self.operation["responses"][str(code)]["content"]["application/json"]["examples"]
            self.assertEqual(shown, examples)

    def test_responses_describe_inference_without_claiming_db_creation(self):
        self.assertEqual(set(self.operation["responses"]), {"200", "400", "401", "500", "503"})
        success = self.operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(success["$ref"], "#/components/schemas/MatchingRecommendationResponse")


if __name__ == "__main__":
    unittest.main()
