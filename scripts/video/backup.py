"""Daily custom-format PostgreSQL dump; remote copies on the separate media volume."""
import hashlib
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote

from ai_service.services.video.storage import VolumeResults, VolumeSettings


def backup_once():
    root = Path(os.environ.get('VIDEO_STATE_DIR', '/data')) / 'backups'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.dump'
    temp, target = root / (name + '.tmp'), root / name
    url = urlsplit(os.environ['VIDEO_DATABASE_URL'])
    env = dict(os.environ, PGHOST=url.hostname or '127.0.0.1', PGPORT=str(url.port or 5432),
               PGUSER=unquote(url.username or ''), PGPASSWORD=unquote(url.password or ''),
               PGDATABASE=url.path.lstrip('/'))
    try:
        subprocess.run(['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--file', str(temp)],
                       env=env, check=True, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    volume = VolumeResults(VolumeSettings.from_env())
    key = 'service-backups/video-db/' + name
    volume.client.upload_file(str(target), volume.settings.volume_id, key)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    volume._verify(key, digest, target.stat().st_size)
    for old in sorted(root.glob('*.dump'))[:-7]:
        old.unlink()
    # Remote retention is deliberately non-destructive; an operator decides the retention period.
    return {'key': key, 'sha256': digest, 'size': target.stat().st_size}


PG_STATE = Path('/data/pg')


def wal_file(timeline, lsn, segment_size):
    """WAL file name holding an LSN such as '0/2000028'."""
    high, low = (int(part, 16) for part in lsn.split('/'))
    segment = ((high << 32) | low) // segment_size
    per_id = 0x100000000 // segment_size
    return f'{timeline:08X}{segment // per_id:08X}{segment % per_id:08X}'


def base_backup_once(keep=2):
    """Physical base backup onto the network volume; restore replays the WAL archive on top of it."""
    import json
    import shutil
    root = PG_STATE / 'base'
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    temp, target = root / (name + '.tmp'), root / name
    subprocess.run(['pg_basebackup', '-h', '/var/run/postgresql', '-U', 'postgres', '-D', str(temp),
                    '-Ft', '-z', '-X', 'fetch', '--checkpoint=fast'],
                   check=True, timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.replace(temp, target)
    backups = sorted(path for path in root.iterdir() if path.is_dir() and not path.name.endswith('.tmp'))
    for old in backups[:-keep]:
        shutil.rmtree(old)
    # WAL older than the oldest kept base backup can never be replayed again.
    oldest = json.loads((backups[-keep:][0] / 'backup_manifest').read_text())['WAL-Ranges'][0]
    size = int(subprocess.run(['psql', '-h', '/var/run/postgresql', '-U', 'postgres', '-Atc', 'SHOW wal_segment_size'],
                              check=True, capture_output=True, text=True).stdout.strip().removesuffix('MB')) * 1024 * 1024
    subprocess.run(['pg_archivecleanup', '-x', '.gz', str(PG_STATE / 'wal'),
                    wal_file(oldest['Timeline'], oldest['Start-LSN'], size)], check=True, stdout=subprocess.DEVNULL)
    return name


def main():
    while True:
        try:
            print('Base backup stored:', base_backup_once(), flush=True)
            result = backup_once()
            print('Database backup verified:', result['key'], flush=True)
        except Exception as exc:
            print('Database backup failed:', type(exc).__name__, flush=True)
            time.sleep(300)
            continue
        time.sleep(86400)

if __name__ == '__main__':
    main()
