"""Create or repair the least-privilege app role; safe to run on every start."""
import json
from pathlib import Path
from urllib.parse import urlsplit
import psycopg
from psycopg import sql
url = urlsplit(json.loads(Path('/data/config/database.json').read_text())['VIDEO_DATABASE_URL'])
with psycopg.connect('dbname=audigo_video user=postgres host=/var/run/postgresql', autocommit=True) as conn:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='audigo'").fetchone()
    verb = 'ALTER' if exists else 'CREATE'
    conn.execute(sql.SQL(verb + ' ROLE audigo LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}').format(sql.Literal(url.password)))
    conn.execute('ALTER DATABASE audigo_video OWNER TO audigo')
    conn.execute('GRANT ALL ON SCHEMA public TO audigo')
