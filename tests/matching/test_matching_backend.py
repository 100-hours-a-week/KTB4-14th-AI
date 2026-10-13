"""Exact backend DTO contract and synchronous local E5 recommendation."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
import httpx
import numpy as np

from ai_service.core.config import Settings
from ai_service.main import create_app
from ai_service.matching.backend import BACKEND_MATCHING_EXAMPLE, display_match_rate

PATH='/api/ai/v1/matches/candidates'
HEADERS={'Authorization':'Bearer test-only'}
ROOT=Path(__file__).resolve().parents[2]


def no_network(request):
    raise AssertionError('Backend matching must not call external providers')


class Encoder:
    def __init__(self): self.calls=[]
    def encode(self,texts):
        self.calls.append(texts)
        return np.array([[1,0]]+[[1,0] if '음식, 자연' in text else [0,1] for text in texts[1:]],dtype=np.float32)


class BackendMatchingTests(unittest.TestCase):
    def app(self, **settings):
        return create_app(settings=Settings(api_token='test-only',**settings),transport=httpx.MockTransport(no_network))

    def test_backend_document_request_returns_exact_final_dto_without_queue_db_or_openai(self):
        encoder=Encoder()
        with patch('ai_service.matching.mysql.MySQLMatchingCandidateSource._read',side_effect=AssertionError('No DB')),TestClient(self.app()) as client:
            client.app.state.backend_matching_recommender.encoder=encoder
            response=client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),{'candidates':[{'user_id':23,'match_rate':100}],'count':1})
        self.assertEqual(len(encoder.calls[0]),2)  # Male candidate is removed before E5.
        self.assertTrue(encoder.calls[0][0].startswith('query: '))
        self.assertTrue(encoder.calls[0][1].startswith('passage: '))
        self.assertNotIn('match_rate',encoder.calls[0][1])

    def test_actual_backend_document_example_is_accepted(self):
        archive=Path('/Users/samrobert/Downloads/KTB4-14th-BE-development.zip')
        if not archive.is_file(): self.skipTest('Backend reference source not present')
        with zipfile.ZipFile(archive) as z:
            doc=z.read('KTB4-14th-BE-development/docs/matching-ai-candidates-integration.md').decode()
        for block in doc.split('```json')[1:]:
            body=json.loads(block.split('```',1)[0])
            if 'request' in body: break
        with TestClient(self.app()) as client:
            client.app.state.backend_matching_recommender.encoder=Encoder()
            response=client.post(PATH,json=body,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(set(response.json()),{'candidates','count'})
        self.assertEqual(response.json()['candidates'][0]['user_id'],23)

    def test_top_three_stable_nickname_ties_and_self_exclusion(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        body['candidates']=[dict(user_id=uid,nickname=name,gender='FEMALE',theme=['NATURE','FOOD'],pace='BALANCED')
            for uid,name in [(1,'본인'),(99,'가'),(23,'가'),(4,'나'),(3,'다'),(2,'라')]]
        with TestClient(self.app()) as client:
            client.app.state.backend_matching_recommender.encoder=Encoder()
            response=client.post(PATH,json=body,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([c['user_id'] for c in response.json()['candidates']],[23,99,4])
        self.assertEqual(response.json()['count'],3)

    def test_empty_incomplete_or_gender_filtered_candidates_return_200_empty_without_model(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        with TestClient(self.app(e5_model_dir=Path('/missing/model'))) as client:
            for candidates in ([],None,[body['candidates'][0]],
                [{**body['candidates'][1],'gender':None}],
                [{**body['candidates'][1],'pace':None}],
                [{**body['candidates'][1],'theme':[]}],
                [{**body['candidates'][1],'theme':None}]):
                with self.subTest(candidates=candidates):
                    response=client.post(PATH,json={**body,'candidates':candidates},headers=HEADERS)
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual(response.json(),{'candidates':[],'count':0})
            self.assertIsNone(client.app.state.backend_matching_recommender.encoder)

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

    def test_model_failures_and_timeout_are_503(self):
        with TestClient(self.app(e5_model_dir=Path('/missing/model'))) as client:
            self.assertEqual(client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS).status_code,503)
        class Broken:
            def encode(self,texts): return np.array([[float('nan'),0]]*len(texts))
        with TestClient(self.app()) as client:
            client.app.state.backend_matching_recommender.encoder=Broken()
            self.assertEqual(client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS).status_code,503)
        async def slow(*args,**kwargs): await asyncio.sleep(.1)
        with TestClient(self.app(model_timeout_seconds=.001)) as client:
            client.app.state.backend_matching_recommender.recommend=slow
            self.assertEqual(client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS).status_code,503)

    def test_more_than_200_candidates_rank_globally_with_configured_limit(self):
        body=deepcopy(BACKEND_MATCHING_EXAMPLE)
        body['candidates']=[dict(user_id=uid,nickname=str(uid),gender='FEMALE',theme=['CULTURE'],pace='BALANCED')
                            for uid in range(2,1002)]
        body['candidates'][-1]['theme']=['NATURE','FOOD']
        encoder=Encoder()
        with TestClient(self.app(matching_candidate_limit=1)) as client:
            client.app.state.backend_matching_recommender.encoder=encoder
            response=client.post(PATH,json=body,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),{'candidates':[{'user_id':1001,'match_rate':100}],'count':1})
        self.assertEqual([len(batch) for batch in encoder.calls],[201]*5)

    def test_display_score_normalization_is_not_success_probability(self):
        self.assertEqual(display_match_rate(-1,(1.,0.,0.)),0)
        self.assertEqual(display_match_rate(0,(1.,0.,0.)),50)
        self.assertEqual(display_match_rate(1,(1.,0.,0.)),100)
        self.assertEqual(display_match_rate(.85,(.85,.1,.05)),85)

    @unittest.skipUnless((ROOT/'model/model_O4.onnx').is_file(),'Pinned local E5 model required')
    def test_real_e5_with_exact_backend_request_without_other_services(self):
        with TestClient(self.app()) as client:
            response=client.post(PATH,json=BACKEND_MATCHING_EXAMPLE,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['count'],1)
        candidate=response.json()['candidates'][0]
        self.assertEqual(candidate['user_id'],23)
        self.assertIsInstance(candidate['match_rate'],int)
        self.assertTrue(0<=candidate['match_rate']<=100)

    def test_openapi_documents_backend_200_and_queue_202(self):
        schema=self.app().openapi()
        sync=schema['paths'][PATH]['post']
        queue=schema['paths']['/api/ai/v1/traveler-match-jobs']['post']
        self.assertIn('200',sync['responses'])
        self.assertEqual(sync['responses']['200']['content']['application/json']['schema']['$ref'],'#/components/schemas/BackendMatchingResponse')
        self.assertIn('202',queue['responses'])
