"""Generate only DB credentials; never print secret values."""
import json, os, secrets
from pathlib import Path

root = Path('/data/config')
password_file = root / 'postgres-password'
if not password_file.exists():
    password_file.write_text(secrets.token_hex(32))
    password_file.chmod(0o600)
app_password_file = root / 'app-password'
if not app_password_file.exists():
    app_password_file.write_text(secrets.token_hex(32))
    app_password_file.chmod(0o600)
password = app_password_file.read_text().strip()
# Only the app user and root can read this file. S3/OpenAI keys stay in Pod env.
config = root / 'database.json'
config.write_text(json.dumps({'VIDEO_DATABASE_URL': f'postgresql://audigo:{password}@127.0.0.1:5432/audigo_video'}))
config.chmod(0o640)
os.chown(config, 0, 10001)
os.chmod(root, 0o750)
os.chown(root, 0, 10001)
