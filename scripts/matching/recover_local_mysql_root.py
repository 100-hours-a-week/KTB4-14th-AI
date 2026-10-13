"""Recover this Mac's Oracle-installed MySQL root account using --init-file.

Run interactively with sudo. Keeps the existing data directory and launchd
configuration. Does not enable skip-grant-tables or change password policies.
"""
import getpass
import os
from pathlib import Path
import plistlib
import pwd
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

PLIST = Path('/Library/LaunchDaemons/com.oracle.oss.mysql.mysqld.plist')
CLIENT = '/usr/local/mysql/bin/mysql'


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_stopped(pid):
    deadline = time.monotonic() + 60
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if alive(pid):
        raise RuntimeError('서버가 정상 종료되지 않았습니다. 강제 종료하지 않고 중단합니다.')


def main():
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise RuntimeError('이 스크립트는 Mac 터미널에서 sudo로 실행해야 합니다.')
    config = plistlib.loads(PLIST.read_bytes())
    args = config['ProgramArguments']
    if args[0] != '/usr/local/mysql/bin/mysqld' or '--user=_mysql' not in args:
        raise RuntimeError('확인된 Oracle MySQL 설치 구성과 다릅니다. 중단합니다.')
    pid_path = next((arg.split('=', 1)[1] for arg in args if arg.startswith('--pid-file=')), None)
    if not pid_path:
        raise RuntimeError('서버 PID 파일을 확인할 수 없습니다.')
    pid = int(Path(pid_path).read_text().strip())
    command = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'comm='], text=True).strip()
    if command != args[0]:
        raise RuntimeError('PID가 MySQL 서버와 일치하지 않습니다.')
    password = getpass.getpass('새 MySQL root 비밀번호: ')
    if not password or any(char in password for char in '\n\r\0'):
        raise RuntimeError('비밀번호가 비어 있거나 지원하지 않는 문자가 있습니다.')
    user = pwd.getpwnam('_mysql')
    label = 'system/' + config['Label']
    # No shell expansion; SQL quoting is independent of shell quoting.
    escaped = password.replace('\\', '\\\\').replace("'", "''")
    with TemporaryDirectory(prefix='audigo-mysql-recovery-', dir='/private/tmp') as directory:
        folder = Path(directory)
        os.chown(folder, user.pw_uid, user.pw_gid)
        init = folder / 'root-init.sql'
        init.write_text("ALTER USER 'root'@'localhost' IDENTIFIED BY '" + escaped + "' ACCOUNT UNLOCK;\n")
        os.chmod(init, 0o600)
        os.chown(init, user.pw_uid, user.pw_gid)
        subprocess.run(['/bin/launchctl', 'bootout', label], check=True)
        process = None
        verified = False
        try:
            wait_stopped(pid)
            # Preserve all original data/config arguments. Recovery accepts only
            # the local Unix socket, then returns to the original launchd service.
            recovery_args = [arg for arg in args if not arg.startswith('--log-error=')]
            recovery_args += ['--init-file=' + str(init), '--skip-networking',
                              '--log-error=' + str(folder / 'recovery.err')]
            process = subprocess.Popen(recovery_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            env = dict(os.environ, MYSQL_PWD=password)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and process.poll() is None:
                result = subprocess.run([CLIENT, '--protocol=SOCKET', '--socket=/tmp/mysql.sock',
                                         '--user=root', '--connect-timeout=2', '--batch', '--skip-column-names',
                                         '-e', 'SELECT CURRENT_USER();'], env=env, capture_output=True, text=True)
                if result.returncode == 0 and result.stdout.strip() == 'root@localhost':
                    verified = True
                    break
                time.sleep(0.3)
            if not verified:
                raise RuntimeError('복구 후 인증에 실패했습니다. 비밀번호 정책 또는 서버 설정 확인이 필요합니다.')
        finally:
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=60)
            if not alive(pid):
                subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(PLIST)], check=True)
        # No plaintext init file remains after leaving the temporary directory.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = subprocess.run([CLIENT, '--protocol=TCP', '--host=127.0.0.1', '--port=3306',
                                 '--user=root', '--connect-timeout=2', '--batch', '--skip-column-names',
                                 '-e', 'SELECT CURRENT_USER();'], env=env, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() == 'root@localhost':
            print('복구 완료: 정상 서비스에서 root TCP 접속을 확인했습니다. 기존 DB는 유지됩니다.')
            return
        time.sleep(0.3)
    raise RuntimeError('정상 서비스 재시작 후 TCP 접속을 확인하지 못했습니다. MySQL 서비스 상태를 확인해주세요.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # Deliberately avoid printing MySQL stderr or init SQL/password.
        print(str(exc) if isinstance(exc, RuntimeError) else '복구 중 시스템 오류가 발생했습니다. 서버 상태를 확인해주세요.', file=sys.stderr)
        sys.exit(1)
