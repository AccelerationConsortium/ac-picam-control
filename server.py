import concurrent.futures
import html
import json
import os
import socket
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("PICAM_SERVER_HOST", "100.88.31.43")
PORT = int(os.environ.get("PICAM_SERVER_PORT", "8081"))

RAW_DEVICES = os.environ.get(
    "PICAM_DEVICES",
    ",".join(
        [
            "rpi-zero2w-stream-cam-4ehl.tail6a1dd7.ts.net",
            "rpi-zero2w-stream-cam-ahdk.tail6a1dd7.ts.net",
            "sdl4-rpi-zero2w-stream-dentistry-a.tail6a1dd7.ts.net",
        ]
    ),
)
DEVICE_HOSTS = [d.strip() for d in RAW_DEVICES.split(",") if d.strip()]

AGENT_PORT = int(os.environ.get("PICAM_AGENT_PORT", "8080"))
REQUEST_TIMEOUT = float(os.environ.get("PICAM_AGENT_TIMEOUT", "5"))
DEBUG = os.environ.get("PICAM_DEBUG", "0") == "1"


def _log(message):
    if DEBUG:
        print(message, flush=True)


JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
}


def _agent_url(host, path):
    return f"http://{host}:{AGENT_PORT}{path}"


def _fetch_json(url, method="GET"):
    _log(f"agent_request method={method} url={url}")
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
        _log(f"agent_response status={resp.status} url={url}")
        return resp.status, body


def _safe_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _device_status(host):
    try:
        status_code, body = _fetch_json(_agent_url(host, "/status"))
        return status_code, _safe_json(body)
    except urllib.error.URLError as exc:
        _log(f"status_error host={host} error={exc}")
        return 0, {"ok": False, "error": str(exc)}
    except Exception as exc:
        _log(f"status_error host={host} error={exc}")
        return 0, {"ok": False, "error": str(exc)}


def _device_action(host, action):
    try:
        status_code, body = _fetch_json(_agent_url(host, f"/{action}"), method="POST")
        return status_code, _safe_json(body)
    except urllib.error.URLError as exc:
        _log(f"action_error host={host} action={action} error={exc}")
        return 0, {"ok": False, "error": str(exc)}
    except Exception as exc:
        _log(f"action_error host={host} action={action} error={exc}")
        return 0, {"ok": False, "error": str(exc)}


def _collect_statuses():
    status_map = {}
    max_workers = len(DEVICE_HOSTS) if DEVICE_HOSTS else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_device_status, host): host for host in DEVICE_HOSTS}
        for future, host in futures.items():
            try:
                _, payload = future.result(timeout=REQUEST_TIMEOUT)
            except concurrent.futures.TimeoutError:
                payload = {"ok": False, "error": "status timeout"}
            except Exception as exc:
                payload = {"ok": False, "error": str(exc)}
            status_map[host] = payload
    return status_map


def _render_page(status_map):
    rows = []
    for host, info in status_map.items():
        ok = info.get("ok")
        status_text = "ok" if ok else "error"
        detail = html.escape(
            info.get("stderr") or info.get("stdout") or info.get("error") or ""
        )
        rows.append(
            f"""
            <tr>
                <td><code>{html.escape(host)}</code></td>
                <td>{status_text}</td>
                <td class="detail">{detail}</td>
                <td>
                    <form method="post" action="/action">
                        <input type="hidden" name="host" value="{html.escape(host)}">
                        <button name="cmd" value="start">Start</button>
                        <button name="cmd" value="stop">Stop</button>
                        <button name="cmd" value="restart">Restart</button>
                    </form>
                </td>
            </tr>
            """
        )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>PiCam Control</title>
  <style>
    body {{ font-family: sans-serif; margin: 24px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; vertical-align: top; }}
    .detail {{ max-width: 420px; word-break: break-word; }}
    form button {{ margin-right: 6px; }}
  </style>
</head>
<body>
  <h1>PiCam Control</h1>
  <p>Central control server (Tailscale-only). Host: <code>{html.escape(HOST)}</code></p>
  <table>
    <thead>
      <tr>
        <th>Device</th>
        <th>Status</th>
        <th>Details</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</body>
</html>"""


class ControlHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        for key, value in JSON_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body, status_code=200):
        data = body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        _log(f"http_get path={path}")
        if path == "/health":
            self._send_json(200, {"ok": True})
            return
        if path == "/":
            status_map = _collect_statuses()
            self._send_html(_render_page(status_map))
            return
        if path == "/status":
            status_map = _collect_statuses()
            self._send_json(200, {"ok": True, "devices": status_map})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        _log(f"http_post path={path}")
        if path != "/action":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8")
        params = parse_qs(body)
        host = (params.get("host") or [""])[0]
        cmd = (params.get("cmd") or [""])[0]

        if host not in DEVICE_HOSTS:
            self._send_json(400, {"ok": False, "error": "Unknown host"})
            return
        if cmd not in ("start", "stop", "restart"):
            self._send_json(400, {"ok": False, "error": "Invalid command"})
            return

        status_code, payload = _device_action(host, cmd)
        self._send_json(200 if status_code else 500, {"ok": True, "result": payload})

    def log_message(self, format, *args):
        return


def main():
    if not DEVICE_HOSTS:
        raise SystemExit("PICAM_DEVICES is empty")
    socket.setdefaulttimeout(REQUEST_TIMEOUT)
    server = HTTPServer((HOST, PORT), ControlHandler)
    print(f"picam-control server listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
