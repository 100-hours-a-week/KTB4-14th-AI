"""Wait for private DB and run a supervised process with its DSN."""
import json, os, sys, time
from pathlib import Path
os.environ.update(json.loads(Path('/data/config/database.json').read_text()))
from ai_service.services.video.jobs import connection, initialize
for attempt in range(60):
    try:
        with connection() as conn:
            conn.execute('SELECT 1')
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit('PostgreSQL did not become ready')
if sys.argv[1] == 'api':
    initialize()
    os.execvp('uvicorn', ['uvicorn', 'ai_service.video_app:app', '--host', '0.0.0.0', '--port', '8000', '--no-access-log'])
elif sys.argv[1] == 'worker':
    initialize()
    os.execvp('python', ['python', '-m', 'scripts.video.worker'])
elif sys.argv[1] == 'backup':
    os.execvp('python', ['python', '-m', 'scripts.video.backup'])
else:
    raise SystemExit('Unknown service')
