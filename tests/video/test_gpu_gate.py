from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'runpod-gpu' / 'gate.py'
TOKEN = 't' * 40
BIG = bytes(range(256)) * 8192  # 2 MiB, larger than one forwarding chunk


def load_gate(monkeypatch, upstream_port, comfy_dir):
    monkeypatch.setenv('COMFYUI_API_TOKEN', TOKEN)
    monkeypatch.setenv('COMFYUI_INTERNAL_PORT', str(upstream_port))
    monkeypatch.setenv('COMFYUI_DIR', str(comfy_dir))
    spec = importlib.util.spec_from_file_location('gate', SCRIPT)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


class FakeComfy(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        FakeComfy.seen.append(('GET', self.path, self.headers.get('Authorization')))
        body = BIG if self.path.startswith('/view') else json.dumps({'queue_running': []}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self.rfile.read(int(self.headers['Content-Length']))
        FakeComfy.seen.append(('POST', self.path, len(data)))
        body = json.dumps({'received': len(data)}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def gate_url(tmp_path, monkeypatch):
    FakeComfy.seen = []
    upstream = serve(FakeComfy)
    gate = load_gate(monkeypatch, upstream.server_address[1], tmp_path / 'ComfyUI')
    front = serve(gate.Gate)
    yield f'http://127.0.0.1:{front.server_address[1]}', tmp_path / 'ComfyUI'
    front.shutdown()
    upstream.shutdown()


def test_requests_without_the_password_never_reach_comfyui(gate_url):
    url, _ = gate_url
    assert httpx.get(url + '/queue').status_code == 401
    assert httpx.get(url + '/view?filename=a.png&type=input', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert FakeComfy.seen == []


def test_authorized_requests_are_forwarded_without_the_password(gate_url):
    url, _ = gate_url
    auth = {'Authorization': f'Bearer {TOKEN}'}
    assert httpx.get(url + '/queue', headers=auth).json() == {'queue_running': []}
    assert httpx.get(url + '/view?filename=clip.mp4&type=output', headers=auth).content == BIG
    assert httpx.post(url + '/upload/image', headers=auth, files={'image': ('p.png', BIG, 'image/png')}).json()['received'] > len(BIG)
    assert all(entry[2] is None for entry in FakeComfy.seen if entry[0] == 'GET')  # token is not passed on


def test_cleanup_removes_only_this_services_files(gate_url):
    url, comfy = gate_url
    for path in ['input/audigo/job1/0/photo.png', 'output/audigo/job1/clip.mp4', 'input/other/keep.png']:
        (comfy / path).parent.mkdir(parents=True, exist_ok=True)
        (comfy / path).write_bytes(b'x')
    assert httpx.post(url + '/_audigo/cleanup').status_code == 401
    response = httpx.post(url + '/_audigo/cleanup', headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.json() == {'removed_files': 2}
    assert not (comfy / 'input' / 'audigo').exists() and not (comfy / 'output' / 'audigo').exists()
    assert (comfy / 'input' / 'other' / 'keep.png').exists()


def test_gate_refuses_to_start_without_a_strong_password(tmp_path, monkeypatch):
    gate = load_gate(monkeypatch, 1, tmp_path)
    monkeypatch.setattr(gate, 'TOKEN', 'short')
    with pytest.raises(SystemExit, match='refusing'):
        gate.main()


def test_worker_purges_media_but_keeps_reports(tmp_path):
    from scripts.video.worker import purge_media
    for name in ['run/photo-0000.png', 'run/film/final.mp4', 'run/score/attempt-0/master.flac', 'run/report.json']:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b'x')
    assert purge_media(tmp_path) == 3
    assert [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file()] == ['run/report.json']
