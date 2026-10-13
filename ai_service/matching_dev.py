"""Synthetic profiles constrained to the supplied backend matching schema.

Regions/enums are source-backed; users and rule relevance labels are synthetic.
Matching has no travel/region/date association, profile budget or introduction.
"""
import hashlib
import json
import random

from ai_service.matching_catalog import DATA_DIR, CATALOG, REGIONS, THEMES, PACES, GENDERS, PREFERRED_GENDERS, LABELS

VERSION = 'matching-backend-v2'
SEED = 20261013


def eligible_profile(request, profile):
    # Backend MatchingProfileRepository + isComplete, then AI gender filtering.
    return (profile['id'] != request['requester_id'] and profile['active']
            and profile['gender'] in GENDERS and profile['pace'] in PACES
            and bool(profile['themes'])
            and (request['gender']=='ANY' or profile['gender']==request['gender']))


def relevance(request, profile):
    requested, offered = set(request['themes']), set(profile['themes'])
    if requested <= offered and request['pace']==profile['pace']:
        return 2,'요청 테마 모두 충족 및 여행 속도 일치'
    if requested & offered and abs(PACES.index(request['pace'])-PACES.index(profile['pace']))<=1:
        return 1,'테마 일부 충족 및 속도 차이 한 단계 이하'
    return 0,'평가 기준의 적합 조건 미충족'


def make_dataset(seed=SEED):
    rng=random.Random(seed)
    fixture=json.loads((DATA_DIR/'matching_request.mock.json').read_text())
    originals={c['user_id']:c for c in fixture['candidates']}
    profiles=[]
    for uid in range(1,1001):
        themes=sorted(rng.sample(THEMES,rng.randint(1,4)))
        pace=rng.choice(PACES)
        gender=rng.choice(GENDERS)
        active=uid%47!=0
        if uid%53==0: gender=None  # Nullable DB field: an incomplete historical profile.
        if uid%59==0: themes=[]
        if uid in originals:
            old=originals[uid]
            # Preserve IDs and legal values only. Unsupported legacy themes,
            # candidate budgets and introductions are not backend columns.
            themes=list(dict.fromkeys(t.upper() for t in old['theme'] if t.upper() in THEMES))
            pace=old['pace'].upper()
            gender=old['gender'].upper()
        profiles.append(dict(id=uid,gender=gender,themes=themes,pace=pace,active=active,
                             status='ACTIVE',deleted_at=None))
    requests,cases,labels=[],[],[]
    for rid in range(1,1001):
        split='DEV' if rid<=600 else 'TEST' if rid<=900 else 'EDGE'
        gender=rng.choice(PREFERRED_GENDERS)
        themes=sorted(rng.sample(THEMES,rng.randint(1,3)))
        pace=rng.choice(PACES)
        budget=rng.choice([(None,None),(0,3000000),(300000,800000),(1000000,2000000)])
        if rid==201:gender,themes,pace,budget='ANY',['NATURE','FOOD'],'BALANCED',(300000,800000)
        request=dict(id=rid,requester_id=rid,gender=gender,themes=themes,pace=pace,
                     budget_min=budget[0],budget_max=budget[1])
        candidates=[p for p in profiles if eligible_profile(request,p)]
        for p in candidates:
            grade,reason=relevance(request,p)
            labels.append((rid,p['id'],grade,reason))
        # No anchor-derived requests: sample only legal backend input values.
        requests.append(request)
        scenario='budget_unavailable' if budget[0] is not None else 'no_budget'
        cases.append((rid,split,scenario,not candidates,False))
    data=dict(profiles=profiles,requests=requests,cases=cases,labels=labels,
              catalog_commit=CATALOG['source_commit'])
    fingerprint=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return data,fingerprint


def insert_dataset(cursor,data,fingerprint,seed=SEED):
    # Regions are copied from backend data.sql; never used to scope matching.
    cursor.executemany('INSERT INTO regions(name,full_name,created_at,updated_at) VALUES (%s,%s,NOW(),NOW())',
                       [(r['name'],r['full_name']) for r in REGIONS])
    cursor.executemany('INSERT INTO users(id,nickname,status,created_at,updated_at,deleted_at) VALUES (%s,%s,%s,NOW(),NOW(),%s)',
        [(p['id'],f"mock_user_{p['id']:04d}",p['status'],p['deleted_at']) for p in data['profiles']])
    cursor.executemany('INSERT INTO matching_profiles(id,user_id,is_active,gender,pace,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW())',
        [(p['id'],p['id'],p['active'],p['gender'],p['pace']) for p in data['profiles']])
    cursor.executemany('INSERT INTO matching_profile_themes(matching_profile_id,theme) VALUES (%s,%s)',
        [(p['id'],t) for p in data['profiles'] for t in p['themes']])
    cursor.executemany('INSERT INTO matching_requests(id,requester_id,preferred_companion_gender,pace,budget_min,budget_max,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,NOW(),NOW())',
        [(r['id'],r['requester_id'],r['gender'],r['pace'],r['budget_min'],r['budget_max']) for r in data['requests']])
    cursor.executemany('INSERT INTO matching_requests_themes(matching_request_id,theme) VALUES (%s,%s)',
        [(r['id'],t) for r in data['requests'] for t in r['themes']])
    cursor.executemany('INSERT INTO matching_evaluation_cases VALUES (%s,%s,%s,%s,%s)',data['cases'])
    cursor.executemany('INSERT INTO matching_evaluation_labels VALUES (%s,%s,%s,%s)',data['labels'])
    cursor.execute('INSERT INTO matching_dataset_meta(id,version,seed,fingerprint) VALUES (1,%s,%s,%s)',(VERSION,seed,fingerprint))
