"""큐 API의 복사용 요청 예시."""
MATCHING_EXAMPLE = {
    'matching_request_id': 201, 'travel_plan_id': 101, 'requester_id': 12,
    'preferred_companion_gender': 'ANY', 'pace': 'BALANCED',
    'budget_min': 300000, 'budget_max': 800000, 'themes': ['자연', '미식'],
    'candidates': [{'user_id': 35, 'pace': 'BALANCED', 'themes': ['자연', '사진'],
                    'budget_min': 300000, 'budget_max': 800000}], 'limit': 10,
}
MATCHING_DB_EXAMPLE = {'matching_request_id': 201, 'requester_id': 201, 'limit': 3}
