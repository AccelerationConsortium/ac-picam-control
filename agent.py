import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

SERVICE_NAME = os.environ.get("PICAM_SERVICE_NAME", "picam")
HOST = os.environ.get("PICAM_AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("PICAM_AGENT_PORT", "8080"))

JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
}


def _run_systemctl(*args):
    result = subprocess.run(
        ["systemctl", *args, SERVICE_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _get_status():
    code, stdout, stderr = _run_systemctl("status", "--no-pager")
    return {
        "ok": code == 0,
        "service": SERVICE_NAME,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": code,
    }


class PicamHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        for key, value in JSON_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"ok": True, "service": SERVICE_NAME})
            return
        if path == "/status":
            payload = _get_status()
            self._send_json(200 if payload["ok"] else 500, payload)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/start":
            code, stdout, stderr = _run_systemctl("start")
            self._send_json(
                200 if code == 0 else 500,
                {"ok": code == 0, "stdout": stdout, "stderr": stderr},
            )
            return
        if path == "/stop":
            code, stdout, stderr = _run_systemctl("stop")
            self._send_json(
                200 if code == 0 else 500,
                {"ok": code == 0, "stdout": stdout, "stderr": stderr},
            )
            return
        if path == "/restart":
            code, stdout, stderr = _run_systemctl("restart")
            self._send_json(
                200 if code == 0 else 500,
                {"ok": code == 0, "stdout": stdout, "stderr": stderr},
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def log_message(self, format, *args):
        return


def main():
    server = HTTPServer((HOST, PORT), PicamHandler)
    print(f"picam-control listening on {HOST}:{PORT} for service '{SERVICE_NAME}'")
    server.serve_forever()


if __name__ == "__main__":
    main()
