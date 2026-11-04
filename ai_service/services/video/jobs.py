"""PostgreSQL job ledger. Only trusted backend requests supply owner IDs."""
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
    'WORKER_INTERRUPTED': '작업 실행기가 반복해서 중단되어 생성을 포기했습니다. 다시 요청해 주세요.',
    'WORKER_PROCESS_FAILED': '영상 생성 프로세스가 비정상 종료되었습니다. 다시 요청해 주세요.',
    'CANCELLED_TEST': '배포 확인용 테스트 작업이라 GPU 실행 전에 취소했습니다.',
}
FAILURE_PREFIXES = [
    ('score_', '자동 작곡한 배경음악이 품질 검사를 통과하지 못했습니다. 다시 요청하거나 음악 없이(NONE) 요청해 주세요.'),
    ('ace_music_', '배경음악을 만드는 중 문제가 생겼습니다. 다시 요청하거나 음악 없이(NONE) 요청해 주세요.'),
    ('music_', '배경음악 파일에 문제가 있습니다. 다른 음악을 고르거나 음악 없이 요청해 주세요.'),
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


def update(job_id, stage, *, status='RUNNING', publication=None, error=None, message=None):
    from psycopg.types.json import Jsonb
    if stage not in STAGES:
        raise ValueError(f'Unknown stage {stage}')
    video_key = publication['assets']['video']['key'] if publication else None
    with connection() as conn:
        conn.execute('''UPDATE video_jobs SET stage=%s,status=%s,publication=%s,video_key=%s,error_code=%s,error_message=%s,
                        completed_at=CASE WHEN %s IN ('COMPLETED','FAILED') THEN now() END,updated_at=now() WHERE id=%s''',
                     (stage, status, Jsonb(publication) if publication else None, video_key, error,
                      message[:500] if message else None, status, job_id))


def set_notice(job_id, text):
    """A user-facing note on an otherwise successful job, such as a music fallback."""
    with connection() as conn:
        conn.execute('UPDATE video_jobs SET notice=%s,updated_at=now() WHERE id=%s', (text[:500], job_id))


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
    conn.execute("""UPDATE video_jobs SET status='QUEUED',stage='QUEUED',updated_at=now()
                    WHERE status='RUNNING' AND attempts < %s""", (MAX_ATTEMPTS,))
    conn.execute("""UPDATE video_jobs SET status='FAILED',stage='FAILED',error_code='WORKER_INTERRUPTED',
                    error_message='작업 실행기가 반복해서 중단되어 생성을 포기했습니다.',completed_at=now(),updated_at=now()
                    WHERE status='RUNNING'""")


def claim(conn):
    return conn.execute("""UPDATE video_jobs SET status='RUNNING',stage='ANALYZING_PHOTOS',attempts=attempts+1,updated_at=now()
        WHERE id=(SELECT id FROM video_jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING *""").fetchone()

if __name__ == '__main__':
    initialize()
