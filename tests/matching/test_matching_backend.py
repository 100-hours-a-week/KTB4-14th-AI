"""백엔드 DTO 검증과 동기 계산식 추천을 확인한다."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
import zipfile
from unittest.mock import patch

from ai_service.services.music import E5MusicRecommender

from fastapi.testclient import TestClient
import httpx

from ai_service.core.config import Settings
from ai_service.main import create_app
from ai_service.api.examples.matching import BACKEND_MATCHING_EXAMPLE

PATH='/api/ai/v1/matching-requests'
HEADERS={'Authorization':'Bearer test-only'}
ROOT=Path(__file__).resolve().parents[2]


def no_network(request):
    raise AssertionError('Backend matching must not call external providers')


class BackendMatchingTests(unittest.TestCase):
    def app(self, **settings):
        return create_app(settings=Settings(api_token='test-only',**settings),transport=httpx.MockTransport(no_network))

    def test_backend_document_request_returns_exact_final_dto_without_queue_db_or_openai(self):
        with TestClient(self.app()) as client:
            client.app.state.jobs=None
            response=client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),{'candidates':[{'user_id':23,'match_rate':100}],'count':1})

    def test_actual_backend_document_example_is_accepted(self):
        archive=Path('/Users/samrobert/Downloads/KTB4-14th-BE-development.zip')
        if not archive.is_file(): self.skipTest('Backend reference source not present')
        with zipfile.ZipFile(archive) as z:
            doc=z.read('KTB4-14th-BE-development/docs/matching-ai-candidates-integration.md').decode()
        for block in doc.split('```json')[1:]:
            body=json.loads(block.split('```',1)[0])
            if 'request' in body: break
        with TestClient(self.app()) as client:
            response=client.post(PATH,json=body,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(set(response.json()),{'candidates','count'})
        self.assertEqual(response.json()['candidates'][0]['user_id'],23)

    def test_top_three_stable_nickname_ties_and_self_exclusion(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        body['candidates']=[dict(user_id=uid,nickname=name,gender='FEMALE',theme=['NATURE','FOOD'],pace='BALANCED')
            for uid,name in [(1,'본인'),(99,'가'),(23,'가'),(4,'나'),(3,'다'),(2,'라')]]
        with TestClient(self.app()) as client:
            response=client.post(PATH,json=body,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([c['user_id'] for c in response.json()['candidates']],[23,99,4])
        self.assertEqual(response.json()['count'],3)

    def test_empty_or_gender_filtered_candidates_return_200_without_model(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        with TestClient(self.app(e5_model_dir=Path('/missing/model'))) as client:
            for candidates in ([],[body['candidates'][0]]):
                response=client.post(PATH,json={**body,'candidates':candidates},headers=HEADERS)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json(),{'candidates':[],'count':0})

    def test_incomplete_candidate_rejects_entire_request(self):
        with TestClient(self.app()) as client:
            for field in ('nickname','gender','theme','pace'):
                for value in ('missing',None):
                    body=deepcopy(BACKEND_MATCHING_EXAMPLE)
                    if value=='missing': body['candidates'][0].pop(field)
                    else: body['candidates'][0][field]=value
                    with self.subTest(field=field,value=value):
                        response=client.post(PATH,json=body,headers=HEADERS)
                        self.assertEqual(response.status_code,400,response.text)
                        self.assertEqual(response.json()['error']['code'],'INVALID_REQUEST')
            for candidates in (None,[{**BACKEND_MATCHING_EXAMPLE['candidates'][1],'theme':[]}]):
                response=client.post(PATH,json={**BACKEND_MATCHING_EXAMPLE,'candidates':candidates},headers=HEADERS)
                self.assertEqual(response.status_code,400,response.text)
            body=deepcopy(BACKEND_MATCHING_EXAMPLE);body.pop('candidates')
            self.assertEqual(client.post(PATH,json=body,headers=HEADERS).status_code,400)

    def test_pace_exclusion_keeps_adjacent_speeds(self):
        for requested in ('RELAXED','BALANCED','PACKED'):
            body=deepcopy(BACKEND_MATCHING_EXAMPLE)
            body['request']['pace']=requested
            body['candidates']=[dict(user_id=uid,nickname=str(uid),gender='FEMALE',
                theme=['NATURE','FOOD'],pace=pace)
                for uid,pace in [(2,'RELAXED'),(3,'BALANCED'),(4,'PACKED')]]
            with self.subTest(requested=requested), TestClient(self.app()) as client:
                response=client.post(PATH,json=body,headers=HEADERS)
                self.assertEqual(response.status_code,200,response.text)
                expected={'RELAXED':{2,3},'BALANCED':{2,3,4},'PACKED':{3,4}}[requested]
                self.assertEqual({c['user_id'] for c in response.json()['candidates']},expected)
        body['request']['pace']='RELAXED'
        body['candidates']=[body['candidates'][-1]]
        with TestClient(self.app(e5_model_dir=Path('/missing/model'))) as client:
            response=client.post(PATH,json=body,headers=HEADERS)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json(),{'candidates':[],'count':0})

    def test_request_limit_defaults_to_three_and_accepts_only_integers_one_to_three(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        body['candidates']=[dict(user_id=uid,nickname=str(uid),gender='FEMALE',
            theme=['NATURE','FOOD'],pace='BALANCED') for uid in range(2,7)]
        with TestClient(self.app()) as client:
            for limit in (1,2,3):
                response=client.post(PATH,json={**body,'limit':limit},headers=HEADERS)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['count'],limit)
                self.assertEqual([c['user_id'] for c in response.json()['candidates']],list(range(2,2+limit)))
            body.pop('limit')
            self.assertEqual(client.post(PATH,json=body,headers=HEADERS).json()['count'],3)
            for limit in (0,4,-1,None,True,1.0,'3'):
                response=client.post(PATH,json={**body,'limit':limit},headers=HEADERS)
                self.assertEqual(response.status_code,400,response.text)

    def test_nickname_and_optional_image_fields_are_metadata_only(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        candidate=body['candidates'][1]
        with TestClient(self.app()) as client:
            for name in ('가'*500,'  나나  '):
                candidate['nickname']=name
                for image in ('missing',None,'https://example.com/profile.png'):
                    if image=='missing': candidate.pop('profile_image_url',None)
                    else: candidate['profile_image_url']=image
                    response=client.post(PATH,json=body,headers=HEADERS)
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual(response.json(),{'candidates':[{'user_id':23,'match_rate':100}],'count':1})
            for name in ('','   ','가'*501,123):
                candidate['nickname']=name
                self.assertEqual(client.post(PATH,json=body,headers=HEADERS).status_code,400)
            candidate['nickname']='나나';candidate['profile_image_url']=123
            self.assertEqual(client.post(PATH,json=body,headers=HEADERS).status_code,400)

    def test_backend_validation_and_service_auth(self):
        mutations=[lambda b:b['request'].update(theme=[]),lambda b:b['request'].update(theme=['NATURE','NATURE']),
            lambda b:b['request'].update(theme=['CITY']),lambda b:b['request'].update(theme=['NATURE','FOOD','REST','SNS']),
            lambda b:b['request'].update(preferred_companion_gender='OTHER'),lambda b:b['request'].update(user_id=True),
            lambda b:b['request'].update(budget_max=3000001),lambda b:b['request'].update(budget_min=None),
            lambda b:b['request'].update(budget_min=900000),lambda b:b['candidates'].append(b['candidates'][0]),
            lambda b:b['candidates'][0].update(theme=['NATURE','FOOD','CULTURE','REST','SNS'])]
        with TestClient(self.app()) as client:
            for mutate in mutations:
                body=deepcopy(BACKEND_MATCHING_EXAMPLE);mutate(body)
                self.assertEqual(client.post(PATH,json=body,headers=HEADERS).status_code,400)
            self.assertEqual(client.post(PATH,json=BACKEND_MATCHING_EXAMPLE).status_code,401)
            self.assertEqual(client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers={'Authorization':'Bearer wrong'}).status_code,401)

    def test_matching_needs_no_model_and_keeps_e5_music_service(self):
        with patch('ai_service.embeddings.e5.E5Encoder.__init__',side_effect=AssertionError('No model for matching')), \
             TestClient(self.app(e5_model_dir=Path('/missing/model'))) as client:
            response=client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json(),{'candidates':[{'user_id':23,'match_rate':100}],'count':1})
            self.assertFalse(hasattr(client.app.state,'backend_matching_recommender'))
            self.assertIsInstance(client.app.state.music_recommender,E5MusicRecommender)
            self.assertIsNone(client.app.state.music_recommender.encoder)

    def test_more_than_200_candidates_rank_globally_with_requested_limit(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        body['candidates']=[dict(user_id=uid,nickname=str(uid),gender='FEMALE',theme=['CULTURE'],pace='BALANCED')
                            for uid in range(2,1002)]
        body['candidates'][-1]['theme']=['NATURE','FOOD']
        body['limit']=1
        with TestClient(self.app()) as client:
            response=client.post(PATH,json=body,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),{'candidates':[{'user_id':1001,'match_rate':100}],'count':1})

    def test_openapi_documents_only_synchronous_backend_contract(self):
        schema=self.app().openapi()
        sync=schema['paths'][PATH]['post']
        self.assertNotIn('/api/ai/v1/traveler-match-jobs',schema['paths'])
        self.assertIn('200',sync['responses'])
        self.assertEqual(sync['responses']['200']['content']['application/json']['schema']['$ref'],'#/components/schemas/BackendMatchingResponse')
        self.assertNotIn('202',sync['responses'])
        self.assertNotIn('422',sync['responses'])
        self.assertIn('400',sync['responses'])
        self.assertEqual(sync['requestBody']['content']['application/json']['schema']['$ref'], '#/components/schemas/BackendMatchingRequest')
        self.assertFalse(sync.get('parameters'))
        request_schema=schema['components']['schemas']['BackendMatchingRequest']
        self.assertEqual(set(request_schema['required']),{'request','candidates'})
        self.assertEqual(request_schema['properties']['limit']['default'],3)
        self.assertEqual(request_schema['properties']['limit']['maximum'],3)
        candidate_schema=schema['components']['schemas']['BackendCandidateProfile']
        self.assertEqual(set(candidate_schema['required']),{'user_id','nickname','gender','theme','pace'})
        self.assertEqual(candidate_schema['properties']['nickname']['maxLength'],500)
        self.assertIn('profile_image_url',candidate_schema['properties'])
        for name in ('MatchingJobRequest', 'MatchingDatabaseJobRequest', 'DatabaseMatchingRecommendationRequest'):
            self.assertNotIn(name, schema['components']['schemas'])

    def test_id_and_flat_legacy_payloads_are_rejected_without_queue(self):
        with TestClient(self.app()) as client:
            client.app.state.jobs=None
            for body in ({'matching_request_id':201,'requester_id':201,'limit':3},
                         {'requester_id':1,'preferred_companion_gender':'Female',
                          'themes':['nature'],'pace':'Balanced','candidates':[],'limit':3}):
                response=client.post(PATH,json=body,headers=HEADERS)
                self.assertEqual(response.status_code,400,response.text)
                self.assertEqual(response.json()['error']['code'],'INVALID_REQUEST')
