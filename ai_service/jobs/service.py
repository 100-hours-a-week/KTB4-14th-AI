"""작업 접수·내구성 있는 발행·결과 조회 서비스."""
from ai_service.jobs.errors import JobApiError
from ai_service.jobs.schemas import JobAccepted
from ai_service.jobs.store import RedisJobStore
from ai_service.jobs.worker import create_celery_app, publish
from ai_service.core.diagnostics import failure

class JobService:
    def __init__(self, store, publisher):
        self.store, self.publisher = store, publisher

    def submit(self, kind, payload, identity, request_id, key):
        doc, created = self.store.reserve(kind, payload, identity, request_id, key)
        # Best-effort low-latency delivery. Redis outbox + beat recovers missed publication.
        if created:
            try:
                score = self.store.client.zscore(self.store.outbox, doc['job_id'])
                self.publisher(doc)
                self.store.dispatched(doc['job_id'], score)
            except Exception as exc:
                failure('queue_delivery_pending',exc,code='QUEUE_PUBLISH_FAILED',reason='queue_publish_failed',
                        request_id=doc['request_id'],job_id=doc['job_id'])
        latest = self.store.get(doc['job_id']) or doc
        return JobAccepted(job_id=doc['job_id'], job_type=doc['job_type'], status=latest['status'],
                           status_url='/api/ai/v1/jobs/' + doc['job_id'])

    def status(self, job_id):
        doc = self.store.get(job_id)
        if not doc or doc['job_type'] != 'ITINERARY_GENERATION':
            raise JobApiError(404, 'JOB_NOT_FOUND', '작업이 없거나 보관 기간이 만료됐습니다.')
        return self.store.public(doc)


def make_job_service(settings):
    if not settings.jobs_redis_url:
        return None
    store = RedisJobStore.from_settings(settings)
    app = create_celery_app(settings)
    return JobService(store, lambda doc: publish(app, doc))
