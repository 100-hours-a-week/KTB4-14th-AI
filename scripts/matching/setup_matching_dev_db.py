"""Create and seed a separate local development DB; never drop existing data."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets

from dotenv import dotenv_values, set_key
import pymysql

import sys

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from ai_service.matching.dev import DATA_DIR, VERSION, SEED, make_dataset, insert_dataset
from ai_service.matching.catalog import REGIONS

LOCAL_ENV = ROOT / '.env.matching-dev'
BUSINESS = ['regions','users','matching_profiles','matching_profile_themes',
            'matching_requests','matching_requests_themes']


def setup(host, port, admin, password, database):
    if not re.fullmatch(r'audigo_matching_(backend_dev|dev|test[a-zA-Z0-9_]*)',database):
        raise ValueError('Only isolated audigo_matching_dev/test databases are supported')
    existing_secrets = dotenv_values(LOCAL_ENV) if LOCAL_ENV.exists() else {}
    rows, fingerprint = make_dataset()
    with pymysql.connect(host=host,port=port,user=admin,password=password,charset='utf8mb4',autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute('CREATE DATABASE IF NOT EXISTS `' + database + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci')
            cur.execute('USE `' + database + '`')
            cur.execute('SHOW TABLES')
            tables = {row[0] for row in cur.fetchall()}
            seeded = False
            if tables:
                if 'matching_dataset_meta' not in tables:
                    raise ValueError('Existing unrecognized DB is not modified')
                cur.execute('SELECT version,fingerprint FROM matching_dataset_meta WHERE id=1')
                prior = cur.fetchone()
                if prior != (VERSION,fingerprint):
                    raise ValueError('Existing dataset differs; create a separate test DB instead of overwriting it')
                seeded = True
            else:
                schema = '\n'.join(line for line in (DATA_DIR/'matching_dev_schema.sql').read_text().splitlines()
                                   if not line.lstrip().startswith('--'))
                for sql in schema.split(';'):
                    if sql.strip(): cur.execute(sql)
                conn.begin()
                try:
                    insert_dataset(cur,rows,fingerprint)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            credentials = {}
            for user, prefix in [('audigo_ai_reader','MATCHING_MYSQL'),('audigo_app','MATCHING_APP'),('audigo_seed','MATCHING_SEED')]:
                cur.execute('SELECT user FROM mysql.user WHERE user=%s AND host=%s',(user,'localhost'))
                exists = cur.fetchone()
                saved = existing_secrets.get(prefix+'_PASSWORD')
                if exists and not saved:
                    raise ValueError('Existing account has no saved dev credential; refusing to reset it: '+user)
                credential = saved or secrets.token_urlsafe(32)
                if not exists:
                    # Preserve recovery credentials before nontransactional
                    # CREATE USER/GRANT, even if a later step fails.
                    LOCAL_ENV.touch(mode=0o600,exist_ok=True)
                    os.chmod(LOCAL_ENV,0o600)
                    set_key(str(LOCAL_ENV),prefix+'_USER',user)
                    set_key(str(LOCAL_ENV),prefix+'_PASSWORD',credential)
                    cur.execute('CREATE USER %s@%s IDENTIFIED BY %s',(user,'localhost',credential))
                for table in BUSINESS:
                    privilege = 'SELECT' if user=='audigo_ai_reader' else 'SELECT,INSERT,UPDATE,DELETE'
                    cur.execute(f'GRANT {privilege} ON `{database}`.`{table}` TO %s@%s',(user,'localhost'))
                if user=='audigo_seed':
                    for table in ['matching_evaluation_cases','matching_evaluation_labels','matching_dataset_meta']:
                        cur.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON `{database}`.`{table}` TO %s@%s',(user,'localhost'))
                credentials[prefix+'_USER'],credentials[prefix+'_PASSWORD'] = user,credential
    values = {**credentials,'MATCHING_MYSQL_HOST':host,'MATCHING_MYSQL_PORT':str(port),
              'MATCHING_MYSQL_DATABASE':database,'MATCHING_MYSQL_QUERY_FILE':str(DATA_DIR/'matching_dev_queries.json')}
    LOCAL_ENV.touch(mode=0o600,exist_ok=True)
    os.chmod(LOCAL_ENV,0o600)
    for key,value in values.items(): set_key(str(LOCAL_ENV),key,str(value))
    env_path = ROOT/'.env'
    env_path.touch(mode=0o600,exist_ok=True)
    os.chmod(env_path,0o600)
    for key in ['MATCHING_MYSQL_HOST','MATCHING_MYSQL_PORT','MATCHING_MYSQL_DATABASE','MATCHING_MYSQL_QUERY_FILE',
                'MATCHING_MYSQL_USER','MATCHING_MYSQL_PASSWORD']:
        set_key(str(env_path),key,values[key])
    set_key(str(env_path),'MATCHING_MYSQL_SOCKET','')
    # Verify reader login and that evaluation labels are not visible to runtime AI.
    with pymysql.connect(host=host,port=port,user=values['MATCHING_MYSQL_USER'],
                         password=values['MATCHING_MYSQL_PASSWORD'],database=database) as conn, conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM users')
        count = cur.fetchone()[0]
        try:
            cur.execute('SELECT COUNT(*) FROM matching_evaluation_labels')
        except pymysql.MySQLError as exc:
            if exc.args[0] != 1142: raise
        else:
            raise ValueError('AI reader unexpectedly has access to evaluation labels')
    summary = dict(database=database,version=VERSION,seed=SEED,fingerprint=fingerprint,
                   users=count,matching_profiles=len(rows['profiles']),matching_requests=len(rows['requests']),
                   regions=len(REGIONS),
                   dev_cases=600,test_cases=300,edge_cases=100,created=not seeded,
                   backend_commit=rows['catalog_commit'],
                   human_reviewed=False,accounts=list(credentials[key] for key in credentials if key.endswith('_USER')))
    output = ROOT/'artifacts'/'matching'
    output.mkdir(parents=True,exist_ok=True)
    (output/'dataset-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=3306)
    parser.add_argument('--admin',default='root')
    parser.add_argument('--database',default='audigo_matching_backend_dev')
    args = parser.parse_args()
    password = os.environ.get('MYSQL_BOOTSTRAP_PASSWORD') or getpass.getpass('MySQL 관리자 암호: ')
    try:
        setup(args.host,args.port,args.admin,password,args.database)
    except pymysql.MySQLError as exc:
        raise SystemExit('MySQL setup failed; error code: '+str(exc.args[0])) from None
