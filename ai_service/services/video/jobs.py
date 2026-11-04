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
}
MAX_ATTEMPTS = 3

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


def recover_interrupted(conn):
    """Requeue jobs a dead worker left RUNNING; give up after MAX_ATTEMPTS claims."""
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
