"""Freeze a ranking configuration on DEV, then evaluate untouched TEST cases.

Synthetic rule labels only; this does not measure real user match acceptance.
E5 embeddings are cached by exact text during offline evaluation, not fabricated.
"""
import argparse
import asyncio
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

from dotenv import set_key
import numpy as np
import pymysql

import sys

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from ai_service.core.config import Settings
from ai_service.embeddings.e5 import (
    E5Encoder
)
from ai_service.matching.backend import ranking_input_for_backend
from ai_service.matching.service import E5MatchingRecommender, eligible_candidates
from scripts.matching.evaluation_data import load_snapshot, backend_input
from scripts.models.download_e5_model import ASSETS, MODEL_REVISION, verify

GRID = [(1.0,0.0,0.0),(0.85,0.10,0.05),(0.70,0.20,0.10),(0.50,0.35,0.15),(0.30,0.50,0.20)]


class CachedEncoder:
    def __init__(self, encoder):
        self.encoder, self.cache = encoder, {}
        self.model_calls = 0
    def encode(self, texts):
        missing = list(dict.fromkeys(text for text in texts if text not in self.cache))
        if missing:
            vectors = self.encoder.encode(missing)
            self.model_calls += 1
            for text, vector in zip(missing,vectors,strict=True):
                self.cache[text] = vector
        return np.asarray([self.cache[text] for text in texts])


def metrics(cases, weights):
    hits, ndcgs, precisions, details = [], [], [], []
    violation, missing, negative_ok = 0, 0, 0
    for case in cases:
        body, candidates, cosine = case['body'], case['candidates'], case['cosine']
        scores = E5MatchingRecommender.combine_scores(body,candidates,cosine,weights)
        order = sorted(range(len(candidates)),key=lambda i:(-float(scores[i]),candidates[i].user_id))[:3]
        ids = [candidates[i].user_id for i in order]
        gold = case['labels']
        actual = {candidate.user_id for candidate in candidates}
        violation += len(actual - set(gold))
        missing += len(set(gold) - actual)
        grades = [gold.get(uid,0) for uid in ids]
        hit = int(any(grade==2 for grade in grades))
        if case['expected_empty']:
            negative_ok += not ids
        else:
            hits.append(hit)
            precisions.append(sum(grade==2 for grade in grades)/3)
            dcg = sum((2**grade-1)/math.log2(rank+2) for rank,grade in enumerate(grades))
            ideal = sorted(gold.values(),reverse=True)[:3]
            idcg = sum((2**grade-1)/math.log2(rank+2) for rank,grade in enumerate(ideal))
            ndcgs.append(dcg/idcg if idcg else 0)
        details.append(dict(matching_request_id=case['matching_request_id'],requester_id=body.requester_id,
                            split=case['split'],scenario=case['scenario'],
                            candidate_count=len(candidates),top3=ids,hit_at_3=hit,
                            expected_empty=case['expected_empty'],positive_count=sum(g==2 for g in gold.values())))
    n, success = len(hits),sum(hits)
    counts = [len(case['candidates']) for case in cases if not case['expected_empty']]
    result = dict(positive_cases=n,hit_at_3=success/n if n else None,hits=success,
                  ndcg_at_3=float(np.mean(ndcgs)) if ndcgs else None,
                  precision_at_3=float(np.mean(precisions)) if precisions else None,
                  hard_condition_violations=violation,missing_eligible_candidates=missing,
                  negative_cases=sum(case['expected_empty'] for case in cases),negative_correct=negative_ok,
                  candidate_counts=dict(min=min(counts),median=float(np.median(counts)),max=max(counts),
                                        more_than_three=sum(count>3 for count in counts)) if counts else None)
    if n:
        z, p = 1.96,success/n
        denominator = 1+z*z/n
        center = (p+z*z/(2*n))/denominator
        delta = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denominator
        result['hit_at_3_wilson_95'] = [center-delta,center+delta]
    return result,details


def select_weights(dev):
    comparisons = []
    for weights in GRID:
        score,_ = metrics(dev,weights)
        comparisons.append(dict(weights=weights,metrics=score))
    # TEST labels/metrics are never used here. Prefer higher E5 weight on ties.
    winner = max(comparisons,key=lambda row:(row['metrics']['hit_at_3'],row['metrics']['ndcg_at_3'],row['weights'][0]))
    return tuple(winner['weights']),comparisons


async def evaluate(*, apply=False, output=None):
    started = time.monotonic()
    settings = Settings.from_env()
    output = output or ROOT/'artifacts'/'matching'
    output.mkdir(parents=True,exist_ok=True)
    for filename,checksum in ASSETS.values():
        if not verify(settings.e5_model_dir/filename,checksum):
            raise RuntimeError('The pinned local E5 assets do not match the evaluation model')
    dataset,metadata,labels,requests,profiles = load_snapshot(ROOT/'.env.matching-dev')
    groups = {split:{row['requester_id'] for row in metadata if row['split']==split} for split in ('DEV','TEST')}
    if groups['DEV'] & groups['TEST']:
        raise RuntimeError('Requester leakage between DEV and TEST')
    encoder = CachedEncoder(E5Encoder(settings.e5_model_dir))
    recommender = E5MatchingRecommender(settings.e5_model_dir,encoder=encoder)
    candidate_cache = {}
    async def load(split):
        loaded = []
        for row in metadata:
            if row['split'] != split: continue
            payload = backend_input(requests[row['matching_request_id']],profiles)
            body = ranking_input_for_backend(payload,3)
            # Reuse equal immutable profiles in this offline run, not fabricated embeddings.
            body.candidates = [candidate_cache.setdefault(
                (c.user_id,c.gender,c.pace,tuple(sorted(c.theme)),c.budget_min,c.budget_max,c.introduction),c)
                for c in body.candidates]
            candidates = eligible_candidates(body)
            cosine = await recommender.cosine_scores(body,candidates) if candidates else np.asarray([])
            loaded.append(dict(**row,body=body,candidates=candidates,cosine=cosine,
                               labels=labels.get(row['matching_request_id'],{})))
            if len(loaded)%100==0: print(f'{split}: {len(loaded)} cases loaded',flush=True)
        return loaded
    dev = await load('DEV')
    weights, comparisons = select_weights(dev)
    frozen = dict(dataset=dataset,weights=weights,selection_split='DEV',
                  frozen_at=datetime.now(timezone.utc).isoformat(),model_revision=MODEL_REVISION)
    manifest = json.dumps(frozen,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
    (output/'ranking-selection.json').write_text(manifest)
    selection_hash = hashlib.sha256(manifest.encode()).hexdigest()
    # Only after weights are frozen do we score the final TEST and negative cases.
    test,edge = await load('TEST'),await load('EDGE')
    selected,details = metrics(test,weights)
    baseline,_ = metrics(test,(1.0,0.0,0.0))
    edge_result,edge_details = metrics(edge,weights)
    passed = (selected['hit_at_3'] is not None and selected['hit_at_3']>=0.9
              and selected['hard_condition_violations']==0 and selected['missing_eligible_candidates']==0
              and edge_result['hard_condition_violations']==0 and edge_result['missing_eligible_candidates']==0)
    if apply:
        # Apply DEV-selected settings independently of whether TEST reaches the target.
        for key,value in zip(('MATCHING_E5_WEIGHT','MATCHING_THEME_WEIGHT','MATCHING_PACE_WEIGHT'),weights,strict=True):
            set_key(str(ROOT/'.env'),key,str(value))
    report = dict(dataset=dataset,target_hit_at_3=0.9,target_met=passed,
        input_source='backend_dto_from_development_db',label_source='synthetic_domain_rules',human_reviewed=False,real_user_acceptance_measured=False,
        selection_sha256=selection_hash,model_revision=MODEL_REVISION,selected_weights=weights,
        dev_comparison=comparisons,test_baseline=baseline,test_selected=selected,edge=edge_result,
        evaluation_requests=len(metadata),unique_dev_requesters=len(groups['DEV']),
        unique_test_requesters=len(groups['TEST']),requester_overlap=0,
        elapsed_seconds=round(time.monotonic()-started,3),embedding_texts=len(encoder.cache),
        embedding_model_calls=encoder.model_calls,embedding_cache='offline_exact_text',
        warning='목데이터의 합성 규칙 정답에 대한 적중률이며 실제 사용자 수락률이나 인간 평가 결과가 아닙니다.')
    (output/'evaluation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    _,dev_details = metrics(dev,weights)
    with (output/'evaluation-details.csv').open('w',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=list(details[0]))
        writer.writeheader()
        writer.writerows(dev_details+details+edge_details)
    markdown = f'''# 동행자 매칭 개발 DB 평가

- 기준: 백엔드 development 커밋 `75d7772b03ecc265527e27c78863b0f457761841`
- 데이터: 사용자 1,000명 / 요청 {len(metadata):,}건 / 고정 시드 {dataset['seed']}
- 개발 600건에서 가중치 선택 후 최종 평가 300건과 예산·프로필 예외 100건 평가
- 선택 가중치(E5·테마·속도): {weights}
- 순수 E5 Hit@3: {baseline['hits']}/{baseline['positive_cases']} ({baseline['hit_at_3']:.1%})
- 선택 방식 Hit@3: {selected['hits']}/{selected['positive_cases']} ({selected['hit_at_3']:.1%})
- 최종 NDCG@3: {selected['ndcg_at_3']:.4f}
- 필수 조건 위반: {selected['hard_condition_violations']}건
- 적격 후보 누락: {selected['missing_eligible_candidates']}건
- 예외 입력 평가: {edge_result['hits']}/{edge_result['positive_cases']} 적중
- 90% 목표: {'충족' if passed else '미충족'}

**이 수치는 합성 규칙 정답을 사용한 목데이터 평가입니다. 인간 검토는 하지 않았으며 실제 사용자 매칭 성사율을 의미하지 않습니다.**
상위 3명 중 요청 테마를 모두 충족하고 속도가 일치하는 후보가 1명 이상이면 적중으로 계산합니다.
개발·최종 평가의 요청자는 겹치지 않으며 개발 DB를 백엔드 DTO로 변환해 평가합니다.
운영 AI 서버는 DB를 조회하지 않으며 평가 정답을 입력에 포함하지 않습니다.
공통 후보 풀을 사용하는 오프라인 평가이며 새로운 사용자 집단에 대한 일반화는 추가 검증이 필요합니다.
요청은 백엔드가 허용하는 테마·속도·성별에서 독립적으로 무작위 생성했습니다.
실제 입력 분포나 후보가 없는 요청을 포함한 전체 서비스 성공률을 측정한 결과가 아닙니다.
실행 시간은 정확한 문장 임베딩 캐시를 사용한 평가 시간이며 실서비스 응답 지연 측정값이 아닙니다.

재현: `./.venv/bin/python scripts/matching/evaluate_matching_dev.py`
상세 수치: [JSON](evaluation-report.json), [요청별 CSV](evaluation-details.csv), [가중치 선택 기록](ranking-selection.json)
'''
    (output/'evaluation-report.md').write_text(markdown)
    print(json.dumps({key:report[key] for key in ('target_met','selected_weights','test_baseline','test_selected','edge','elapsed_seconds')},ensure_ascii=False),flush=True)
    return passed


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true',help='Apply DEV-selected weights to local .env')
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    try:
        passed = asyncio.run(evaluate(apply=args.apply,output=args.output))
    except pymysql.MySQLError as exc:
        raise SystemExit('MySQL evaluation failed; error code: '+str(exc.args[0])) from None
    raise SystemExit(0 if passed else 1)
