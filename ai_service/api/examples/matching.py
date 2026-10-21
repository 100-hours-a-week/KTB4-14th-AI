"""실제 매칭 요청·응답 계약에 맞춘 Swagger 예시."""
from ai_service.schemas.matching import MatchingErrorResponse


BACKEND_MATCHING_EXAMPLE={
    'request':{'matching_request_id':10,'user_id':1,'preferred_companion_gender':'FEMALE',
               'theme':['NATURE','FOOD'],'pace':'BALANCED','budget_min':100000,'budget_max':800000},
    'candidates':[
        {'user_id':22,'nickname':'매칭후보A','profile_image_url':None,'gender':'MALE','theme':['CULTURE','SNS'],'pace':'RELAXED'},
        {'user_id':23,'nickname':'매칭후보B','profile_image_url':None,'gender':'FEMALE','theme':['NATURE','FOOD'],'pace':'BALANCED',
         'budget_min':100000,'budget_max':800000},
    ],
    'limit':3,
}


BUDGET_MATCHING_EXAMPLE = {
    'request': {**BACKEND_MATCHING_EXAMPLE['request']},
    'candidates': [
        {'user_id':uid,'nickname':f'예산후보{uid}','gender':'FEMALE',
         'theme':['NATURE','FOOD'],'pace':'BALANCED','budget_min':low,'budget_max':high}
        for uid,low,high in [(23,100000,800000),(24,450000,1150000),(25,900000,1600000)]
    ],
    'limit': 3,
}


MATCHING_REQUEST_EXAMPLES = {
    'backend': {'summary': '여성 선호 → 남성 22 제외·여성 23 추천', 'value': BACKEND_MATCHING_EXAMPLE},
    'budget': {'summary': '같은 테마·속도에서 예산에 따라 100·98·95점', 'value': BUDGET_MATCHING_EXAMPLE},
}


def error_example(code, message, *, retryable=False, details=None):
    return {'error': {'code': code, 'message': message, 'retryable': retryable,
                      'details': details or []}, 'request_id': 'req_matching_example'}


def validation_example(summary, field, error_type):
    return {'summary': summary, 'value': error_example(
        'INVALID_REQUEST', '입력값을 확인해 주세요.',
        details=[{'field': field, 'type': error_type}])}


MATCHING_RESPONSES = {
    200: {
        'description': '추천 성공. 점수순으로 최대 limit명 반환하며 후보가 없으면 빈 배열을 반환합니다.',
        'content': {'application/json': {'examples': {
            'recommended': {'summary': '추천 성공 (기본 요청 예시의 결과)',
                'value': {'candidates': [{'user_id': 23, 'match_rate': 100}], 'count': 1}},
            'top_three': {'summary': '추천 성공 (후보 3명)',
                'value': {'candidates': [{'user_id': 23, 'match_rate': 100},
                    {'user_id': 24, 'match_rate': 90}, {'user_id': 25, 'match_rate': 60}], 'count': 3}},
            'empty': {'summary': '추천할 후보 없음 (정상 응답)',
                'value': {'candidates': [], 'count': 0}},
            'budget_adjusted': {'summary': '예산 요청 예시의 결과 (예산 영향 최대 5점)',
                'value': {'candidates': [{'user_id':23,'match_rate':100},
                    {'user_id':24,'match_rate':98},{'user_id':25,'match_rate':95}], 'count':3}},
        }}},
    },
    400: {
        'model': MatchingErrorResponse,
        'description': '요청 검증 실패. 필수 값 누락·null, 불완전한 후보, limit 범위 초과 등을 거절합니다.',
        'content': {'application/json': {'examples': {
            'gender_null': validation_example('동행 성별이 null',
                'body.request.preferred_companion_gender', 'literal_error'),
            'theme_null': validation_example('여행 테마가 null', 'body.request.theme', 'list_type'),
            'pace_null': validation_example('여행 속도가 null', 'body.request.pace', 'literal_error'),
            'candidates_null': validation_example('후보 목록이 null', 'body.candidates', 'list_type'),
            'nickname_missing': validation_example('첫 번째 후보의 닉네임 누락',
                'body.candidates.0.nickname', 'missing'),
            'limit_exceeded': validation_example('추천 수가 3명 초과', 'body.limit', 'less_than_equal'),
        }}},
    },
    401: {
        'model': MatchingErrorResponse,
        'description': '서비스 Bearer 토큰이 없거나 올바르지 않습니다.',
        'content': {'application/json': {'examples': {
            'unauthorized': {'summary': '서비스 인증 실패', 'value': error_example(
                'UNAUTHORIZED', '인증 정보를 확인해 주세요.')},
        }}},
    },
    500: {
        'model': MatchingErrorResponse,
        'description': '예상하지 못한 서버 내부 오류입니다.',
        'content': {'application/json': {'examples': {
            'internal_error': {'summary': '서버 내부 오류', 'value': error_example(
                'INTERNAL_SERVER_ERROR', '서버 내부 오류가 발생했습니다.')},
        }}},
    },
    503: {
        'model': MatchingErrorResponse,
        'description': '서버의 서비스 인증 토큰(AUDIGO_API_TOKEN)이 설정되지 않았습니다.',
        'content': {'application/json': {'examples': {
            'service_unavailable': {'summary': '서비스 인증 설정 누락', 'value': error_example(
                'QUEUE_UNAVAILABLE', '요청을 처리할 수 없습니다.', retryable=True)},
        }}},
    },
}
