"""운영 로그용 한국어 설명. 입력값이나 외부 예외 문자열을 인용하지 않는다."""

REASONS = {
    'service_token_missing': ('서비스 인증 설정이 없어 요청을 처리하지 못했습니다.', 'AUDIGO_API_TOKEN이 설정되지 않았습니다.', 'AI 서버와 백엔드의 서비스 토큰 설정을 확인하고 설정 변경 후 서버를 재시작하세요.'),
    'service_token_invalid': ('서비스 인증에 실패했습니다.', 'Authorization 헤더가 없거나 Bearer 토큰이 서버 설정과 일치하지 않습니다.', '백엔드의 Authorization: Bearer 헤더와 양쪽 서비스 토큰이 같은지 확인하세요.'),
    'method_not_allowed': ('요청한 HTTP 메서드를 사용할 수 없습니다.', '해당 API 경로에서 허용하지 않는 메서드로 요청했습니다.', 'Swagger의 HTTP 메서드를 확인하세요. 동행자 추천은 POST로 호출하세요.'),
    'openai_api_key_missing': ('일정 생성 서비스 설정이 누락됐습니다.', 'OpenAI API 키가 없습니다.', 'OPENAI_API_KEY 또는 OPEN_API_KEY를 확인하세요.'),
    'kakao_api_key_missing': ('장소 조회 서비스 설정이 누락됐습니다.', '카카오 REST API 키가 없습니다.', 'KAKAO_REST_API_KEY를 확인하세요.'),
    'provider_keys_missing': ('일정 생성에 필요한 서비스 설정이 누락됐습니다.', 'OpenAI 또는 카카오 API 키가 없습니다.', 'OpenAI와 카카오 API 키가 실행 환경에 설정됐는지 확인하세요.'),
    'generation_timeout': ('일정 생성 제한 시간을 초과했습니다.', '전체 일정 생성이 제한 시간 안에 끝나지 않았습니다.', '같은 request_id의 단계 로그에서 지연된 외부 서비스와 마지막 완료 단계를 확인하세요.'),
    'stream_timeout': ('일정 생성 스트리밍 제한 시간을 초과했습니다.', 'SSE 일정 생성이 전체 제한 시간 안에 끝나지 않았습니다.', '같은 request_id의 마지막 단계와 외부 API 응답 지연을 확인하세요.'),
    'music_timeout': ('음악 추천 제한 시간을 초과했습니다.', '음악 검색·검증·추론이 제한 시간 안에 끝나지 않았습니다.', 'YouTube 검색 지연과 음악 모델 추론 시간을 확인하세요.'),
    'provider_timeout': ('외부 서비스 응답 대기 시간을 초과했습니다.', '외부 서비스가 제한 시간 안에 응답하지 않았습니다.', '외부 서비스 상태와 연결 제한 시간을 확인하세요.'),
    'openai_request_failed': ('OpenAI 요청에 실패했습니다.', '일정 생성용 외부 API 요청이 정상 완료되지 않았습니다.', '외부 HTTP 상태, API 키 권한, 호출 한도와 서버 네트워크를 확인하세요.'),
    'openai_refusal': ('모델이 일정 생성 요청을 거절했습니다.', 'OpenAI 응답을 일정 결과로 사용할 수 없습니다.', '요청 조건과 모델의 응답 상태를 확인하세요.'),
    'music_model_refused': ('모델이 음악 추천 요청을 거절했습니다.', '음악 단계의 모델 응답을 사용할 수 없습니다.', '같은 request_id의 음악 단계와 모델 응답 상태를 확인하세요.'),
    'music_model_load_failed': ('음악 추천 모델을 불러오지 못했습니다.', 'E5 모델 파일이나 실행 환경을 확인해야 합니다.', 'E5_MODEL_DIR의 model_O4.onnx·tokenizer.json 및 ONNX Runtime을 확인하세요. 동행자 매칭은 이 모델을 사용하지 않습니다.'),
    'music_model_inference_failed': ('음악 추천 모델 계산에 실패했습니다.', 'E5 추론 결과의 크기·숫자 값이 유효하지 않거나 추론 중 오류가 발생했습니다.', '모델 파일과 ONNX Runtime 버전, 같은 request_id의 오류 유형을 확인하세요.'),
    'music_candidates_unavailable': ('추천할 음악 후보가 없습니다.', '검색·필터링 후 남은 음악 후보가 없습니다.', '여행 조건과 음악 검색 결과, 후보 제외 조건을 확인하세요.'),
    'music_video_not_verified': ('음악 영상 검증에 실패했습니다.', '추천 후보에서 사용할 수 있는 영상을 확인하지 못했습니다.', '영상 공개 상태와 YouTube 메타데이터 조회 결과를 확인하세요.'),
    'youtube_oembed_failed': ('YouTube 영상 정보 조회에 실패했습니다.', '영상 메타데이터를 정상적으로 확인하지 못했습니다.', '영상 공개 상태와 YouTube 연결 상태를 확인하세요.'),
    'youtube_search_failed': ('YouTube 음악 검색에 실패했습니다.', '음악 검색 요청이 정상 완료되지 않았습니다.', '검색 차단 여부와 네트워크, yt-dlp 실행 상태를 확인하세요.'),
    'youtube_search_launch_failed': ('YouTube 검색 도구를 실행하지 못했습니다.', 'yt-dlp 프로세스를 시작하지 못했습니다.', 'yt-dlp 설치와 서버 실행 권한을 확인하세요.'),
    'youtube_search_timeout_or_invalid': ('YouTube 검색 결과를 사용할 수 없습니다.', '검색 제한 시간을 초과했거나 결과 형식이 올바르지 않습니다.', 'yt-dlp 버전, 검색 지연과 결과 형식을 확인하세요.'),
    'kakao_places_request_failed': ('카카오 장소 검색에 실패했습니다.', '장소 검색 요청이 정상 완료되지 않았습니다.', '카카오 REST API 키, 호출 한도와 네트워크를 확인하세요.'),
    'kakao_route_request_failed': ('카카오 자동차 경로 조회에 실패했습니다.', '경로 요청이 정상 완료되지 않았습니다.', '카카오 경로 API 권한과 호출 한도, 네트워크를 확인하세요.'),
    'kakao_route_response_invalid': ('카카오 경로 응답을 사용할 수 없습니다.', '경로 응답의 필수 항목이나 숫자 값이 유효하지 않습니다.', '출발·도착 장소와 경로 API 응답 형식을 확인하세요.'),
    'kakao_transit_route_failed': ('대중교통 경로 조회에 실패했습니다.', '해당 구간의 대중교통 경로를 확인하지 못했습니다.', '구간과 이동수단 조건, 경로 제공자의 상태를 확인하세요.'),
    'kakao_walk_route_failed': ('도보 경로 조회에 실패했습니다.', '해당 구간의 도보 경로를 확인하지 못했습니다.', '장소 좌표와 도보 이동 가능 여부, 경로 제공자의 상태를 확인하세요.'),
    'region_not_found': ('여행 지역을 찾지 못했습니다.', '전달된 지역명으로 위치를 확인할 수 없습니다.', '지역명과 카카오 지역 검색 결과를 확인하세요.'),
    'ambiguous_region': ('여행 지역을 하나로 특정하지 못했습니다.', '전달된 지역명에 여러 검색 결과가 대응합니다.', '시·도와 시·군·구를 포함한 구체적인 지역명을 전달하세요.'),
    'region_center_invalid': ('여행 지역 좌표를 사용할 수 없습니다.', '지역 중심 좌표가 없거나 유효하지 않습니다.', '지역 검색 결과의 좌표를 확인하세요.'),
    'no_place_candidates': ('일정에 넣을 장소 후보가 없습니다.', '장소 조회·필터링 후 사용할 후보가 남지 않았습니다.', '지역·취향·필수 장소 조건과 검색 결과를 확인하세요.'),
    'places_validation_failed': ('장소 배치 검증에 실패했습니다.', '선택한 장소가 일정 조건을 충족하지 못했습니다.', '단계와 detail에서 날짜별 장소 수와 필수 장소 조건을 확인하세요.'),
    'insufficient_trip_time': ('여행 시간이 부족해 일정을 만들지 못했습니다.', '도착·출발 시각 사이에 필요한 방문 시간을 배정할 수 없습니다.', '여행 시간을 늘리거나 필수 방문 장소를 줄이세요.'),
    'routes_exceed_trip_time': ('이동 시간을 반영한 일정이 여행 시간을 초과했습니다.', '방문 시간과 실제 이동 시간을 함께 배정할 수 없습니다.', '여행 시간을 늘리거나 이동 거리·필수 장소 조건을 조정하세요.'),
    'too_many_required_places': ('필수 방문 장소가 일정 허용 범위를 초과했습니다.', '주어진 날짜와 속도에서 필수 장소를 모두 배치할 수 없습니다.', '필수 장소 수를 줄이거나 여행 기간을 늘리세요.'),
    'accommodation_unavailable': ('숙소 후보를 확보하지 못했습니다.', '일정에 필요한 숙소를 찾거나 선택하지 못했습니다.', '해당 날짜의 숙소 후보 수와 지역 조건을 확인하세요.'),
    'accommodation_anchor_missing': ('숙소 배치 기준 장소가 없습니다.', '숙소를 연결할 방문 장소를 확인하지 못했습니다.', '숙소 단계 직전의 장소 선택 결과를 확인하세요.'),
    'accommodation_schedule_invalid': ('숙소를 포함한 일정 검증에 실패했습니다.', '숙소 배치 후 날짜별 시간 조건을 충족하지 못했습니다.', 'detail의 날짜와 숙소 배치·도착 시각을 확인하세요.'),
    'required_place_not_found': ('필수 방문 장소를 찾지 못했습니다.', '필수 장소를 장소 제공자에서 확인하지 못했습니다.', '백엔드가 전달한 장소 ID와 실제 장소 존재 여부를 확인하세요.'),
    'required_place_details_missing': ('필수 방문 장소 정보가 부족합니다.', '장소 ID·좌표 등 일정 생성에 필요한 정보가 없습니다.', '필수 장소의 필수 필드를 백엔드 DTO에 포함하세요.'),
    'required_place_category_unsupported': ('필수 방문 장소의 분류를 사용할 수 없습니다.', '지원하는 장소 분류에 해당하지 않습니다.', '관광·식당·숙소 등 요청 스키마의 허용 분류를 확인하세요.'),
    'required_restaurant_hours_missing': ('필수 식당의 영업시간을 확인하지 못했습니다.', '영업시간 데이터에 필수 식당 정보가 없습니다.', '식당 ID와 영업시간 데이터의 연결을 확인하세요.'),
    'restaurant_hours_data_unavailable': ('식당 영업시간 데이터를 불러오지 못했습니다.', '영업시간 파일이 없거나 내용이 유효하지 않습니다.', 'RESTAURANT_HOURS_FILE 경로와 JSON 형식을 확인하세요.'),
    'restricted_transport_route_missing': ('선택한 이동수단으로 경로를 확보하지 못했습니다.', '이동수단 제한을 충족하는 경로가 없습니다.', '해당 구간의 이동수단이나 방문 장소를 조정하세요.'),
    'transport_day_conflict': ('날짜별 이동수단 설정이 충돌합니다.', '같은 날짜에 서로 다른 이동수단이 지정됐습니다.', '날짜별 이동수단 지정이 중복되지 않도록 수정하세요.'),
    'transport_day_out_of_range': ('이동수단 지정 날짜가 여행 범위를 벗어났습니다.', '여행 기간 밖의 날짜에 이동수단이 지정됐습니다.', '이동수단 날짜를 여행 도착·출발 날짜 범위에 맞추세요.'),
    'transport_day_range_invalid': ('이동수단 날짜 범위가 올바르지 않습니다.', '날짜 범위의 시작·종료 설정을 사용할 수 없습니다.', '이동수단 날짜 범위와 여행 기간을 확인하세요.'),
    'job_storage_unavailable': ('일정 작업 저장소에 연결하지 못했습니다.', 'SQS 중복 처리 기록을 읽거나 갱신하지 못했습니다.', '선택 설정 JOBS_REDIS_URL과 Redis 연결·가용 상태를 확인하세요. SQS 작업 큐와는 별도 저장소입니다.'),
}

CODES = {
    'INVALID_REQUEST': ('요청 검증에 실패했습니다.', '요청 필드의 누락·자료형·허용 범위를 확인해야 합니다.', 'errors의 field·message_ko·action_ko를 보고 백엔드 요청 DTO를 수정하세요.'),
    'UNAUTHORIZED': REASONS['service_token_invalid'],
    # 매칭 503의 기존 응답 코드 호환용이며 별도 큐 사용을 뜻하지 않는다.
    'QUEUE_UNAVAILABLE': ('서비스 요청을 처리하지 못했습니다.', '서비스 인증 설정 또는 처리 조건을 확인해야 합니다.', 'reason과 서비스 토큰 설정을 확인하세요. 매칭은 동기 API입니다.'),
    'PROVIDER_KEYS_MISSING': REASONS['provider_keys_missing'],
    'NOT_FOUND': ('요청한 API 경로가 없습니다.', '등록되지 않은 URL 또는 HTTP 경로로 요청했습니다.', '배포한 API 경로와 백엔드 호출 URL을 확인하세요. 현재 매칭 경로는 /api/ai/v1/matches/candidates입니다.'),
    'AI_ITINERARY_GENERATION_FAILED': ('요청 조건에 맞는 일정을 만들지 못했습니다.', '장소·시간·숙소·경로 조건 검증에 실패했습니다.', 'reason·detail과 단계 로그에서 실패한 조건을 확인하세요.'),
    'AI_MUSIC_RECOMMENDATION_FAILED': ('추천 가능한 음악을 선택하지 못했습니다.', '검색 또는 영상 검증 후 사용할 음악이 없습니다.', '음악 단계의 reason과 후보 검색·검증 결과를 확인하세요.'),
    'AI_SERVICE_UNAVAILABLE': ('추천 서비스 처리를 완료하지 못했습니다.', '외부 서비스·모델·연결 제한 시간에 문제가 발생했습니다.', 'reason과 단계 로그에서 실패한 제공자·설정·제한 시간을 확인하세요.'),
    'ROUTING_SERVICE_UNAVAILABLE': ('이동 경로를 확인하지 못했습니다.', '경로 제공자 요청을 정상 처리하지 못했습니다.', 'reason과 구간별 경로 조회 로그를 확인하세요.'),
    'INTERNAL_SERVER_ERROR': ('서버 내부 오류가 발생했습니다.', '예상하지 못한 예외가 발생했습니다. 상세 위치는 frames에 기록됩니다.', '동일한 request_id로 로그를 모으고 error_type·frames의 파일·줄 번호를 확인하세요.'),
    'HTTP_ERROR': ('HTTP 요청을 처리하지 못했습니다.', '요청이 허용된 API 처리 조건을 충족하지 못했습니다.', 'http_status·method·path와 Swagger의 호출 조건을 확인하세요.'),
}


def failure_description(code, reason=None):
    message, cause, action = REASONS.get(reason, CODES.get(str(code).upper(), CODES['INTERNAL_SERVER_ERROR']))
    return {'message_ko': message, 'cause_ko': cause, 'action_ko': action}


FIELD_LABELS = {
    'preferred_companion_gender': '선호 동행 성별', 'theme': '여행 테마',
    'pace': '여행 속도', 'candidates': '후보 목록', 'nickname': '후보 닉네임',
    'gender': '후보 성별', 'user_id': '사용자 ID', 'limit': '추천 수',
    'budget_min': '최소 예산', 'budget_max': '최대 예산',
    'profile_image_url': '프로필 이미지 주소',
    'matching_request_id': '매칭 요청 ID',
}


def validation_details(errors, *, matching=False):
    result = []
    for error in errors[:10]:
        loc = error['loc']
        field = '.'.join(map(str,loc))
        leaf = str(loc[-1]) if loc else 'body'
        label = FIELD_LABELS.get(leaf, '요청 항목')
        kind = error['type']
        ctx = error.get('ctx',{})
        message = {
            'missing': '필수 값이 누락됐습니다.', 'list_type': '배열 형식으로 전달해야 합니다.',
            'string_type': '문자열 형식으로 전달해야 합니다.', 'int_type': '정수 형식으로 전달해야 합니다.',
            'literal_error': '허용된 선택값이 아닙니다.', 'extra_forbidden': '허용하지 않는 필드입니다.',
            'json_invalid': 'JSON 문법이 올바르지 않습니다.', 'value_error': '필드 간 조건이나 값 검증에 실패했습니다.',
            'string_too_short': '문자열이 너무 짧습니다.', 'string_too_long': '문자열이 너무 깁니다.',
            'too_short': '항목 수가 최소 개수보다 적습니다.', 'too_long': '항목 수가 최대 개수를 초과했습니다.',
        }.get(kind,'자료형·형식·허용 범위를 벗어났습니다.')
        if kind!='missing' and error.get('input') is None:
            message = '필수 값이 null로 전달됐습니다.'
        for error_kind, key, template in (
            ('less_than_equal','le','{value} 이하여야 합니다.'),
            ('less_than','lt','{value} 미만이어야 합니다.'),
            ('greater_than_equal','ge','{value} 이상이어야 합니다.'),
            ('greater_than','gt','{value}보다 커야 합니다.'),
            ('string_too_long','max_length','최대 {value}자까지 허용합니다.'),
            ('string_too_short','min_length','최소 {value}자 이상이어야 합니다.'),
            ('too_long','max_length','최대 {value}개까지 허용합니다.'),
            ('too_short','min_length','최소 {value}개 이상이어야 합니다.'),
        ):
            if kind==error_kind and type(ctx.get(key)) in (int,float):
                message = template.format(value=ctx[key])
        if matching and kind=='value_error':
            validation_error = ctx.get('error')
            args = validation_error.args if isinstance(validation_error,ValueError) else ()
            known_messages = {
                'Both budgets must be supplied or omitted': '최소·최대 예산을 함께 전달하거나 함께 생략해야 합니다.',
                'budget_min must not exceed budget_max': '최소 예산이 최대 예산보다 큽니다.',
                'candidate user_id must be unique': '후보 사용자 ID가 중복됐습니다.',
                'themes must be unique': '여행 테마가 중복됐습니다.',
                'nickname must not be blank': '후보 닉네임이 공백만으로 구성됐습니다.',
            }
            if args and isinstance(args[0],str) and args[0] in known_messages:
                message = known_messages[args[0]]
        action = '요청 스키마의 자료형과 필수·허용 범위를 확인하세요.'
        if matching:
            action = {
                'preferred_companion_gender': 'ANY, FEMALE, MALE 중 하나를 전달하세요.',
                'pace': 'RELAXED, BALANCED, PACKED 중 하나를 전달하세요.',
                'gender': '후보 성별은 FEMALE 또는 MALE로 전달하세요.',
                'theme': 'NATURE, FOOD, CULTURE, REST, SNS, ACTIVITY를 배열로 전달하세요. 요청은 1~3개, 후보는 1~4개이며 중복은 허용하지 않습니다.',
                'candidates': '후보 목록은 배열로 전달하세요. 추천할 후보가 없으면 []를 전달하세요.',
                'nickname': '후보 닉네임은 공백만 있는 값을 제외한 1~500자 문자열로 전달하세요.',
                'limit': 'limit은 1~3 정수로 전달하세요. 생략하면 3명을 추천합니다.',
                'budget_min': '예산은 둘 다 생략하거나 0 ≤ 최소 예산 ≤ 최대 예산 ≤ 3000000으로 전달하세요.',
                'budget_max': '예산은 둘 다 생략하거나 0 ≤ 최소 예산 ≤ 최대 예산 ≤ 3000000으로 전달하세요.',
            }.get(leaf,action)
        if kind=='json_invalid':
            label = '요청 본문'
            action = 'JSON의 따옴표·쉼표·괄호를 확인하세요.'
        limits = {key:ctx[key] for key in ('min_length','max_length','ge','gt','le','lt')
                  if key in ctx and type(ctx[key]) in (int,float)}
        result.append({'field':field,'type':kind,'message_ko':f'{label}: {message}',
                       'action_ko':action, **({'limits':limits} if limits else {})})
    return result
