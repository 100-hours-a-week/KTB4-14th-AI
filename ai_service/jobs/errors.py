from ai_service.core.errors import ApiError
from ai_service.jobs.schemas import JobError, JobErrorResponse


class JobApiError(ApiError):
    def __init__(self, status_code, code, message, *, retryable=False, details=None):
        super().__init__(status_code, code, message)
        self.retryable = retryable
        self.details = details or []


def is_job_request(request):
    path = request.url.path
    return path in {'/api/ai/v1/itinerary-jobs', '/api/ai/v1/matches/candidates', '/api/ai/v1/traveler-match-jobs'} or path.startswith('/api/ai/v1/jobs/')


def error_response(request, code, message, *, retryable=False, details=None):
    return JobErrorResponse(error=JobError(code=code, message=message, retryable=retryable,
                                          details=details or []),
                            request_id=getattr(request.state, 'request_id', 'unknown')).model_dump(mode='json')
