"""영상 작업 대기열, 상태, 실제 사용한 GPU를 PostgreSQL에 기록한다."""
import os
from contextlib import contextmanager

# Ordered user-facing progress; FAILED may replace any running stage.
STAGES = {
    'QUEUED': '대기 중',
    'ANALYZING_PHOTOS': '사진 분석 중',
    'GENERATING_VIDEO': 'AI 영상 생성 중',
    'EDITING_VIDEO': '영상 편집 중',
    'SAVING': '저장 중',
    'COMPLETED': '완료',
    'FAILED': '실패',
    'CANCELLED': '취소됨',
}
MAX_ATTEMPTS = 3

# User-facing Korean explanation for a pipeline failure code: exact codes first, then the code's prefix.
FAILURE_MESSAGES = {
    'too_many_photos_for_duration': '사진 수에 비해 영상 길이가 짧습니다. 사진을 줄이거나 영상 길이를 늘려 다시 요청해 주세요.',
    'insufficient_usable_photos': '영상에 쓸 수 있는 사진이 부족합니다. 흐리거나 어두운 사진 대신 선명한 사진을 더 올려 주세요.',
    'photo_quality_rejected': '영상에 쓸 수 있는 사진이 부족합니다. 흐리거나 어두운 사진 대신 선명한 사진을 더 올려 주세요.',
    'insufficient_usable_memory_scenes': '영상으로 만들 만한 장면을 충분히 찾지 못했습니다. 여행의 여러 순간이 담긴 사진을 더 올려 주세요.',
    'runpod_no_ready_pod': '영상을 만드는 GPU 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.',
    'video_gpu_not_selectable': '이 GPU 모델은 현재 선택할 수 없습니다. 다른 GPU로 다시 요청해 주세요.',
    'WORKER_INTERRUPTED': '작업 실행기가 반복해서 중단되어 생성을 포기했습니다. 다시 요청해 주세요.',
    'WORKER_PROCESS_FAILED': '영상 생성 프로세스가 비정상 종료되었습니다. 다시 요청해 주세요.',
    'final_video_music_missing': '최종 영상에서 배경음악을 확인하지 못했습니다. 다시 요청해 주세요.',
    'CANCELLED_TEST': '배포 확인용 테스트 작업이라 GPU 실행 전에 취소했습니다.',
}
FAILURE_PREFIXES = [
    ('score_', '배경음악을 준비하거나 검증하는 중 문제가 생겼습니다. 다시 요청해 주세요.'),
    ('ace_music_', '배경음악을 만드는 중 문제가 생겼습니다. 다시 요청해 주세요.'),
    ('music_', '배경음악 파일에 문제가 있습니다. 다른 음악을 골라 다시 요청해 주세요.'),
    ('photo_', '읽을 수 없거나 너무 큰 사진이 있습니다. JPEG·PNG·WebP 사진(20MB 이하)으로 다시 올려 주세요.'),
    ('memory_film_', 'AI가 만든 장면이 품질 검사를 충분히 통과하지 못했습니다. 다른 사진을 섞어 다시 요청해 주세요.'),
    ('memory_', '사진을 장면으로 묶는 중 문제가 생겼습니다. 다시 요청해 주세요.'),
    ('travel_story', '여행 이야기 구성(AI 사진 분석)에 실패했습니다. 잠시 후 다시 요청해 주세요.'),
    ('quality_', 'AI 품질 검수 단계에서 문제가 생겼습니다. 잠시 후 다시 요청해 주세요.'),
    ('comfyui_', '영상을 만드는 GPU 서버에서 문제가 생겼습니다. 잠시 후 다시 요청해 주세요.'),
    ('runpod_storage_', '완성 영상을 저장하는 중 문제가 생겼습니다. 다시 요청해 주세요.'),
    ('storage_', '완성 영상을 저장하는 중 문제가 생겼습니다. 다시 요청해 주세요.'),
    ('runpod_', '영상을 만드는 GPU 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.'),
    ('premium_', '영상 편집(자막·음악 합치기) 단계에서 문제가 생겼습니다. 다시 요청해 주세요.'),
    ('final_video_', '완성 영상 검사에서 문제가 발견되었습니다. 다시 요청해 주세요.'),
    ('media_', '영상 편집 프로그램 처리 중 문제가 생겼습니다. 다시 요청해 주세요.'),
]
UNKNOWN_FAILURE = '영상을 만드는 중 예상하지 못한 문제가 생겼습니다. 다시 요청해 주세요.'


def failure_message(code):
    """Korean explanation for a failure code such as 'score_quality_failed'."""
    if not code:
        return None
    if code in FAILURE_MESSAGES:
        return FAILURE_MESSAGES[code]
    return next((message for prefix, message in FAILURE_PREFIXES if code.startswith(prefix)), UNKNOWN_FAILURE)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS video_uploads (
 id uuid PRIMARY KEY, owner_id bigint NOT NULL CHECK(owner_id > 0),
 storage_key text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS video_jobs (
 id uuid PRIMARY KEY, owner_id bigint NOT NULL CHECK(owner_id > 0),
 request jsonb NOT NULL, status text NOT NULL DEFAULT 'QUEUED',
 stage text NOT NULL DEFAULT 'QUEUED', publication jsonb, error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS travel_plan_id bigint;
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS photo_keys text[];
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS video_key text;
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS error_message text;
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS notice text;
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS cancel_requested boolean NOT NULL DEFAULT false;
-- When each stage began, e.g. {"ANALYZING_PHOTOS": "2026-11-05T06:20:00Z"}, so screens can show time per stage.
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS stage_times jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE video_jobs ADD COLUMN IF NOT EXISTS assigned_gpu_type text;
CREATE INDEX IF NOT EXISTS video_jobs_queue ON video_jobs(created_at) WHERE status='QUEUED';
CREATE INDEX IF NOT EXISTS video_jobs_owner ON video_jobs(owner_id, created_at);
CREATE TABLE IF NOT EXISTS video_worker (
 id integer PRIMARY KEY CHECK(id=1), state text NOT NULL,
 heartbeat_at timestamptz NOT NULL DEFAULT now()
);
'''

@contextmanager
def connection():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['VIDEO_DATABASE_URL'], row_factory=dict_row, connect_timeout=5, options='-c statement_timeout=15000 -c lock_timeout=5000') as conn:
        yield conn


def initialize():
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(41420261105)")
        conn.execute(SCHEMA)


def update(job_id, stage, *, status='RUNNING', publication=None, error=None, message=None, notice=None):
    from psycopg.types.json import Jsonb
    if stage not in STAGES:
        raise ValueError(f'Unknown stage {stage}')
    video_key = publication['assets']['video']['key'] if publication else None
    with connection() as conn:
        conn.execute('''UPDATE video_jobs SET stage=%s,status=%s,publication=%s,video_key=%s,error_code=%s,error_message=%s,notice=%s,
                        completed_at=CASE WHEN %s IN ('COMPLETED','FAILED') THEN now() END,updated_at=now(),
                        stage_times=CASE WHEN stage_times ? %s::text THEN stage_times ELSE stage_times || jsonb_build_object(%s::text, now()) END
                        WHERE id=%s''',
                     (stage, status, Jsonb(publication) if publication else None, video_key, error,
                      message[:500] if message else None, notice[:500] if notice else None, status, stage, stage, job_id))


CANCELLED_MESSAGE = '요청에 따라 영상 생성을 취소했습니다.'


def cancel(job_id, owner):
    """Cancel an owner's job. A queued job ends at once; a running job is flagged and the worker stops it.
    Returns the new status, or None when the job does not exist, or 'FINISHED' when it already ended."""
    with connection() as conn:
        row = conn.execute('SELECT status FROM video_jobs WHERE id=%s AND owner_id=%s FOR UPDATE', (job_id, owner)).fetchone()
        if row is None:
            return None
        if row['status'] in ('QUEUED', 'HELD'):
            conn.execute("""UPDATE video_jobs SET status='CANCELLED',stage='CANCELLED',error_message=%s,
                            completed_at=now(),updated_at=now() WHERE id=%s""", (CANCELLED_MESSAGE, job_id))
            return 'CANCELLED'
        if row['status'] == 'RUNNING':
            conn.execute('UPDATE video_jobs SET cancel_requested=true,updated_at=now() WHERE id=%s', (job_id,))
            return 'CANCELLING'
        return 'FINISHED'


def mark_cancelled(conn, job_id):
    conn.execute("""UPDATE video_jobs SET status='CANCELLED',stage='CANCELLED',error_message=%s,completed_at=now(),
                    updated_at=now() WHERE id=%s AND status='RUNNING'""", (CANCELLED_MESSAGE, job_id))


def recover_interrupted(conn):
    """Requeue jobs a dead worker left RUNNING; give up after MAX_ATTEMPTS claims."""
    conn.execute("""UPDATE video_jobs SET status='CANCELLED',stage='CANCELLED',error_message=%s,completed_at=now(),
                    updated_at=now() WHERE status='RUNNING' AND cancel_requested""", (CANCELLED_MESSAGE,))
    conn.execute("""UPDATE video_jobs SET status='QUEUED',stage='QUEUED',assigned_gpu_type=NULL,updated_at=now()
                    WHERE status='RUNNING' AND attempts < %s""", (MAX_ATTEMPTS,))
    conn.execute("""UPDATE video_jobs SET status='FAILED',stage='FAILED',error_code='WORKER_INTERRUPTED',
                    error_message='작업 실행기가 반복해서 중단되어 생성을 포기했습니다.',completed_at=now(),updated_at=now()
                    WHERE status='RUNNING'""")


def claim(conn, job_id, assigned_gpu_type=None):
    """GPU 준비를 확인한 작업만 가져온다. 대기 중 취소된 요청은 건너뛴다."""
    return conn.execute("""UPDATE video_jobs SET status='RUNNING',stage='ANALYZING_PHOTOS',attempts=attempts+1,
        assigned_gpu_type=%s,updated_at=now(),stage_times=jsonb_build_object('QUEUED', created_at, 'ANALYZING_PHOTOS', now())
        WHERE id=%s AND status='QUEUED' RETURNING *""", (assigned_gpu_type, job_id)).fetchone()

if __name__ == '__main__':
    initialize()
