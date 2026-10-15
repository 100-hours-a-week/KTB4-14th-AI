"""고정된 테마·속도 계산 규칙을 합성 개발 데이터의 정답과 비교해 평가한다.

DB는 읽기 전용으로 조회하며 모델 로드·가중치 선택·.env 수정은 수행하지 않는다.
"""
import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean, median
import sys
import time

import pymysql

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from ai_service.services.matching import is_pace_compatible
from ai_service.services.matching import RANKING_VERSION, rank_candidates
from scripts.matching.evaluation_data import load_snapshot, backend_input


def metrics(cases):
    hits, ndcgs, precisions, details = [], [], [], []
    violation, missing, negative_ok = 0, 0, 0
    for case in cases:
        body, ranked = case['body'], case['ranked']
        candidates = [candidate for _,candidate in ranked]
        ids = [candidate.user_id for _,candidate in ranked[:body.limit]]
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
        details.append(dict(matching_request_id=case['matching_request_id'],requester_id=body.request.user_id,
                            split=case['split'],scenario=case['scenario'],
                            candidate_count=len(candidates),top3=ids,hit_at_3=hit,
                            expected_empty=case['expected_empty'],positive_count=sum(g==2 for g in gold.values())))
    n, success = len(hits),sum(hits)
    counts = [len(case['ranked']) for case in cases if not case['expected_empty']]
    result = dict(positive_cases=n,hit_at_3=success/n if n else None,hits=success,
                  ndcg_at_3=mean(ndcgs) if ndcgs else None,
                  precision_at_3=mean(precisions) if precisions else None,
                  hard_condition_violations=violation,missing_eligible_candidates=missing,
                  negative_cases=sum(case['expected_empty'] for case in cases),negative_correct=negative_ok,
                  candidate_counts=dict(min=min(counts),median=median(counts),max=max(counts),
                                        more_than_three=sum(count>3 for count in counts)) if counts else None)
    if n:
        z, p = 1.96,success/n
        denominator = 1+z*z/n
        center = (p+z*z/(2*n))/denominator
        delta = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denominator
        result['hit_at_3_wilson_95'] = [center-delta,center+delta]
    return result,details



def build_cases(metadata, labels, requests, profiles):
    profiles_by_id = {p['user_id']:p for p in profiles}
    cases = []
    for row in metadata:
        body = backend_input(requests[row['matching_request_id']], profiles)
        # Historical DB labels predate opposite-pace exclusion. Adapt gold
        # membership independently of the actual calculated ranking.
        gold = {uid:grade for uid,grade in labels.get(row['matching_request_id'],{}).items()
                if is_pace_compatible(body.request.pace,profiles_by_id[uid]['pace'])}
        cases.append(dict(**{**row,'expected_empty':not gold},body=body,
                          ranked=rank_candidates(body),labels=gold))
    return cases


def evaluate(*, output=None):
    started = time.monotonic()
    output = output or ROOT/'artifacts'/'matching'/RANKING_VERSION
    dataset,metadata,labels,requests,profiles = load_snapshot(ROOT/'.env.matching-dev')
    groups = {split:{r['requester_id'] for r in metadata if r['split']==split} for split in ('DEV','TEST')}
    if groups['DEV'] & groups['TEST']:
        raise RuntimeError('Requester leakage between DEV and TEST')
    cases = build_cases(metadata,labels,requests,profiles)
    results,details = {},[]
    for split in ('DEV','TEST','EDGE'):
        results[split],rows = metrics([case for case in cases if case['split']==split])
        details.extend(rows)
    test,edge = results['TEST'],results['EDGE']
    passed = (test['hit_at_3'] is not None and test['hit_at_3'] >= .9
              and all(r['hard_condition_violations']==0 and r['missing_eligible_candidates']==0
                      and r['negative_correct']==r['negative_cases'] for r in results.values()))
    report = dict(dataset=dataset,ranking_version=RANKING_VERSION,e5_used=False,
        score_formula='80 * theme_overlap / requested_theme_count + pace_points; round half up',
        pace_points={'same':20,'adjacent':10,'opposite':'excluded'},
        input_source='backend_dto_from_development_db',filter_policy='exclude_opposite_pace',
        label_source='synthetic_domain_rules',human_reviewed=False,real_user_acceptance_measured=False,
        target_hit_at_3=.9,target_met=passed,dev=results['DEV'],test=test,edge=edge,
        evaluation_requests=len(metadata),unique_dev_requesters=len(groups['DEV']),
        unique_test_requesters=len(groups['TEST']),requester_overlap=0,
        elapsed_seconds=round(time.monotonic()-started,3),
        warning='합성 규칙 정답에 대한 적중률이며 실제 사용자 매칭 성사율이 아닙니다.')
    output.mkdir(parents=True,exist_ok=True)
    (output/'evaluation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if details:
        with (output/'evaluation-details.csv').open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(details[0]),lineterminator='\n')
            writer.writeheader();writer.writerows(details)
    markdown=f'''# 동행자 계산식 매칭 개발 DB 평가

- 추천 방식: `{RANKING_VERSION}` (E5 미사용)
- 점수: 테마 일치 비율 × 80 + 속도 동일 20점 / 한 단계 차이 10점
- 양극단 속도는 제외하며 점수는 반올림합니다.
- 고정 계산식으로 DEV 600건·TEST 300건·EDGE 100건 평가 (가중치 선택 없음)
- TEST Hit@3: {test['hits']}/{test['positive_cases']} ({test['hit_at_3']:.1%})
- TEST NDCG@3: {test['ndcg_at_3']:.4f}
- 필수 조건 위반: {test['hard_condition_violations']}건
- 적격 후보 누락: {test['missing_eligible_candidates']}건
- 90% 목표: {'충족' if passed else '미충족'}

합성 규칙 정답에 대한 평가이며 실제 사용자 수락률이나 매칭 성사율은 측정하지 않았습니다.
상위 3명 중 요청 테마를 모두 충족하고 속도가 일치하는 후보가 있으면 적중입니다.
개발·최종 평가의 요청자는 겹치지 않습니다. DB는 읽기 전용으로 조회하며 정답을 API 입력에 포함하지 않습니다.
기존 정답 후보 집합에도 양극단 속도 제외 정책을 적용했습니다.
운영 API는 DB를 조회하지 않고 백엔드가 공급한 후보만 계산합니다.

재현: `./.venv/bin/python scripts/matching/evaluate_matching_dev.py`
[상세 JSON](evaluation-report.json) · [요청별 결과](evaluation-details.csv)
'''
    (output/'evaluation-report.md').write_text(markdown)
    print(json.dumps({key:report[key] for key in ('ranking_version','target_met','test','edge','elapsed_seconds')},ensure_ascii=False))
    return passed


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        passed=evaluate(output=args.output)
    except pymysql.MySQLError as exc:
        raise SystemExit('MySQL evaluation failed; error code: '+str(exc.args[0])) from None
    raise SystemExit(0 if passed else 1)
