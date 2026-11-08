"""Password gate in front of ComfyUI on a GPU Pod. ComfyUI has no authentication of its own.

The Pod publishes only this gate (port 8189) instead of ComfyUI's port 8188, so the internet cannot reach
ComfyUI, its uploaded photos or its outputs without the shared token:

    COMFYUI_API_TOKEN=... python3 gate.py      # started by the Pod's start command before /start.sh

Every request must carry `Authorization: Bearer <COMFYUI_API_TOKEN>` or, behind a RunPod serverless endpoint
(whose edge takes Authorization for the RunPod API key), `X-Audigo-Token: <COMFYUI_API_TOKEN>`; others get 401
and are never forwarded. GET /ping needs no token: the serverless load balancer polls it and gets 200 once
ComfyUI answers and the boot script has finished (marker file AUDIGO_READY_FILE), 204 before that.
POST /_audigo/cleanup deletes this service's uploaded photos and generated files from ComfyUI's input and output
folders, which live on the persistent network volume. Standard library only.
"""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import shutil
import sys

COMFY = ('127.0.0.1', int(os.environ.get('COMFYUI_INTERNAL_PORT', 8188)))
COMFY_DIR = Path(os.environ.get('COMFYUI_DIR', '/workspace/runpod-slim/ComfyUI'))
# The pipeline uploads under input/audigo/... and saves with filename prefixes audigo/... Pods started by gpu_boot.sh
# keep ComfyUI's input and output on the Pod's own disk (COMFYUI_IO_DIR) instead of the network volume.
IO_DIR = Path(os.environ.get('COMFYUI_IO_DIR', str(COMFY_DIR)))
OWNED = [IO_DIR / 'input' / 'audigo', IO_DIR / 'output' / 'audigo']
HOP_BY_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
              'transfer-encoding', 'upgrade', 'host', 'authorization', 'content-length', 'x-audigo-token'}
CHUNK = 1024 * 1024
TOKEN = os.environ.get('COMFYUI_API_TOKEN', '')
READY_FILE = Path(os.environ.get('AUDIGO_READY_FILE', '/local/ready'))


def comfy_up():
    connection = HTTPConnection(*COMFY, timeout=5)
    try:
        connection.request('GET', '/system_stats')
        return connection.getresponse().status == 200
    except OSError:
        return False
    finally:
        connection.close()


def cleanup(folders=OWNED):
    """Delete owned upload and output trees; returns how many files were removed."""
    removed = 0
    for folder in folders:
        if folder.is_dir():
            removed += sum(1 for path in folder.rglob('*') if path.is_file())
            shutil.rmtree(folder)
    return removed


class Gate(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):  # Paths can contain upload names; keep the Pod log quiet.
        pass

    def _authorized(self):
        bearer = self.headers.get('Authorization', '').encode()
        gate = self.headers.get('X-Audigo-Token', '').encode()
        return bool(TOKEN) and (hmac.compare_digest(bearer, f'Bearer {TOKEN}'.encode())
                                or hmac.compare_digest(gate, TOKEN.encode()))

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        if self.path == '/ping' and self.command == 'GET':
            if READY_FILE.exists() and comfy_up():
                return self._reply(200, {'status': 'healthy'})
            self.send_response(204)
            self.end_headers()
            return
        if not self._authorized():
            return self._reply(401, {'error': 'unauthorized'})
        if self.path == '/_audigo/cleanup' and self.command == 'POST':
            return self._reply(200, {'removed_files': cleanup()})
        length = int(self.headers.get('Content-Length') or 0)
        upstream = HTTPConnection(*COMFY, timeout=600)
        try:
            upstream.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            upstream.putheader('Host', f'{COMFY[0]}:{COMFY[1]}')
            for name, value in self.headers.items():
                if name.lower() not in HOP_BY_HOP:
                    upstream.putheader(name, value)
            upstream.putheader('Content-Length', str(length))
            upstream.endheaders()
            remaining = length
            while remaining:
                block = self.rfile.read(min(CHUNK, remaining))
                if not block:
                    break
                upstream.send(block)
                remaining -= len(block)
            response = upstream.getresponse()
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP:
                    self.send_header(name, value)
            body_length = response.getheader('Content-Length')
            if body_length is None:
                self.send_header('Transfer-Encoding', 'chunked')
            else:
                self.send_header('Content-Length', body_length)
            self.end_headers()
            for block in iter(lambda: response.read(CHUNK), b''):
                if body_length is None:
                    self.wfile.write(f'{len(block):x}\r\n'.encode() + block + b'\r\n')
                else:
                    self.wfile.write(block)
            if body_length is None:
                self.wfile.write(b'0\r\n\r\n')
        except OSError:
            # ComfyUI not up yet (or restarting): look like a not-ready server, never an open one.
            self._reply(502, {'error': 'comfyui_unavailable'})
        finally:
            upstream.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


def main():
    if len(TOKEN) < 32:
        sys.exit('COMFYUI_API_TOKEN (32+ characters) is required; refusing to start an open gate')
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('GATE_PORT', 8189))), Gate).serve_forever()


if __name__ == '__main__':
    main()
