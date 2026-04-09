import concurrent.futures
import html
import json
import os
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

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

YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")
YT_APP_NAME = os.environ.get("YT_APP_NAME", "ac-picam-control")
YT_PRIVACY = os.environ.get("YT_PRIVACY", "private")
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def _log(message):
    if DEBUG:
        print(message, flush=True)


JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
}

STREAM_STATE = {}


def _agent_url(host, path):
    return f"http://{host}:{AGENT_PORT}{path}"


def _fetch_json(url, method="GET"):
    _log(f"agent_request method={method} url={url}")
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
        _log(f"agent_response status={resp.status} url={url}")
        return resp.status, body


def _post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, body


def _safe_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _youtube_request(method, path, access_token, params=None, body=None):
    url = f"{YOUTUBE_API_BASE}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {"Authorization": f"Bearer {access_token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read() if exc.fp else b""
        body = body_bytes.decode("utf-8", errors="replace")
        _log(f"youtube_error status={exc.code} body={body}")
        raise


def _get_access_token():
    if not (YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN):
        raise RuntimeError("YouTube credentials not configured on server")
    data = urlencode(
        {
            "client_id": YT_CLIENT_ID,
            "client_secret": YT_CLIENT_SECRET,
            "refresh_token": YT_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("YouTube token response missing access_token")
    return token


def _create_youtube_stream(access_token, title):
    _, payload = _youtube_request(
        "POST",
        "/liveStreams",
        access_token,
        params={"part": "snippet,cdn,contentDetails,status"},
        body={
            "snippet": {"title": title, "description": f"Stream for {title}"},
            "cdn": {
                "frameRate": "variable",
                "ingestionType": "rtmp",
                "resolution": "variable",
            },
            "contentDetails": {"isReusable": True},
        },
    )
    ingestion = (payload.get("cdn") or {}).get("ingestionInfo") or {}
    ffmpeg_url = ingestion.get("ingestionAddress")
    stream_key = ingestion.get("streamName")
    if not (ffmpeg_url and stream_key):
        raise RuntimeError("YouTube stream response missing ingestion info")
    return payload["id"], ffmpeg_url, stream_key


def _create_youtube_broadcast(access_token, title):
    for status in ("active", "upcoming"):
        items = _list_broadcasts(access_token, status)
        for item in items:
            snippet = item.get("snippet") or {}
            if snippet.get("title") == title and item.get("id"):
                return item["id"]
    scheduled_start = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _, payload = _youtube_request(
        "POST",
        "/liveBroadcasts",
        access_token,
        params={"part": "snippet,contentDetails,status"},
        body={
            "snippet": {"title": title, "scheduledStartTime": scheduled_start},
            "status": {"privacyStatus": YT_PRIVACY, "selfDeclaredMadeForKids": False},
            "contentDetails": {"enableAutoStart": True, "enableAutoStop": False},
        },
    )
    return payload["id"]


def _bind_broadcast(access_token, broadcast_id, stream_id):
    _youtube_request(
        "POST",
        "/liveBroadcasts/bind",
        access_token,
        params={
            "part": "id,contentDetails,status",
            "id": broadcast_id,
            "streamId": stream_id,
        },
        body=None,
    )


def _list_broadcasts(access_token, status):
    _, payload = _youtube_request(
        "GET",
        "/liveBroadcasts",
        access_token,
        params={
            "part": "snippet,status,contentDetails",
            "broadcastStatus": status,
            "maxResults": 50,
        },
        body=None,
    )
    return payload.get("items") or []


def _get_broadcast(access_token, broadcast_id):
    _, payload = _youtube_request(
        "GET",
        "/liveBroadcasts",
        access_token,
        params={"part": "snippet,status,contentDetails", "id": broadcast_id},
        body=None,
    )
    items = payload.get("items") or []
    if not items:
        raise RuntimeError("Broadcast not found")
    return items[0]


def _find_broadcast_for_title(access_token, title):
    for status in ("active", "upcoming", "completed"):
        try:
            items = _list_broadcasts(access_token, status)
        except Exception as exc:
            _log(f"broadcast_lookup_error status={status} error={exc}")
            continue
        for item in items:
            snippet = item.get("snippet") or {}
            if snippet.get("title") == title:
                return item
    return None


def _device_stream_stop(host):
    return _post_json(_agent_url(host, "/stream/stop"), {})


def _start_stream_for_host(host):
    access_token = _get_access_token()
    title = host.split(".")[0]
    stream_id, ffmpeg_url, stream_key = _create_youtube_stream(access_token, title)
    broadcast_id = _create_youtube_broadcast(access_token, title)
    _bind_broadcast(access_token, broadcast_id, stream_id)
    status_code, body = _post_json(
        _agent_url(host, "/stream/start"),
        {"ffmpeg_url": ffmpeg_url, "stream_key": stream_key},
    )
    broadcast = _get_broadcast(access_token, broadcast_id)
    thumbnails = (broadcast.get("snippet") or {}).get("thumbnails") or {}
    thumbnail_url = (
        thumbnails.get("high")
        or thumbnails.get("medium")
        or thumbnails.get("default")
        or {}
    ).get("url")
    watch_url = f"https://www.youtube.com/watch?v={broadcast_id}"
    STREAM_STATE[host] = {
        "broadcast_id": broadcast_id,
        "stream_id": stream_id,
        "ffmpeg_url": ffmpeg_url,
        "stream_key": stream_key,
        "watch_url": watch_url,
        "thumbnail_url": thumbnail_url,
        "title": title,
    }
    payload = _safe_json(body)
    payload.update(
        {
            "broadcast_id": broadcast_id,
            "stream_id": stream_id,
            "ffmpeg_url": ffmpeg_url,
            "stream_key": stream_key,
            "watch_url": watch_url,
            "thumbnail_url": thumbnail_url,
        }
    )
    return status_code, payload


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
        if (
            host not in STREAM_STATE
            and YT_CLIENT_ID
            and YT_CLIENT_SECRET
            and YT_REFRESH_TOKEN
        ):
            try:
                access_token = _get_access_token()
                title = host.split(".")[0]
                match = _find_broadcast_for_title(access_token, title)
                if match:
                    broadcast_id = match.get("id")
                    thumbnails = (match.get("snippet") or {}).get("thumbnails") or {}
                    thumbnail_url = (
                        thumbnails.get("high")
                        or thumbnails.get("medium")
                        or thumbnails.get("default")
                        or {}
                    ).get("url")
                    STREAM_STATE[host] = {
                        "broadcast_id": broadcast_id,
                        "stream_id": (match.get("contentDetails") or {}).get(
                            "boundStreamId"
                        ),
                        "watch_url": f"https://www.youtube.com/watch?v={broadcast_id}",
                        "thumbnail_url": thumbnail_url,
                        "title": (match.get("snippet") or {}).get("title", title),
                    }
            except Exception as exc:
                _log(f"broadcast_lookup_failed host={host} error={exc}")

        stream_info = STREAM_STATE.get(host, {})
        watch_url = stream_info.get("watch_url")
        thumbnail_url = stream_info.get("thumbnail_url")
        if watch_url and thumbnail_url:
            thumb = f'<a href="{html.escape(watch_url)}" target="_blank" rel="noreferrer"><img src="{html.escape(thumbnail_url)}" alt="preview"></a>'
        else:
            thumb = '<span class="muted">—</span>'
        rows.append(
            f"""
            <tr>
                <td><code>{html.escape(host)}</code></td>
                <td>{status_text}</td>
                <td class="thumb">{thumb}</td>
                <td class="detail">{detail}</td>
                <td>
                    <form method="post" action="/action">
                        <input type="hidden" name="host" value="{html.escape(host)}">
                        <button name="cmd" value="start_stream">Start Stream</button>
                        <button name="cmd" value="stop_stream">Stop Stream</button>
                        <button name="cmd" value="start">Start Service</button>
                        <button name="cmd" value="stop">Stop Service</button>
                        <button name="cmd" value="restart">Restart Service</button>
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
    .detail {{ max-width: 420px; max-height: 140px; overflow: auto; word-break: break-word; white-space: pre-wrap; }}
    .thumb img {{ width: 120px; border-radius: 6px; display: block; }}
    .muted {{ color: #777; }}
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
        <th>Preview</th>
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
        if path.startswith("/desired-state/"):
            host = path.split("/desired-state/", 1)[1]
            info = STREAM_STATE.get(host)
            if info:
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "should_stream": True,
                        "ffmpeg_url": info.get("ffmpeg_url"),
                        "stream_key": info.get("stream_key"),
                    },
                )
            else:
                self._send_json(200, {"ok": True, "should_stream": False})
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
        if cmd not in ("start", "stop", "restart", "start_stream", "stop_stream"):
            self._send_json(400, {"ok": False, "error": "Invalid command"})
            return

        if cmd == "start_stream":
            status_code, payload = _start_stream_for_host(host)
            self._send_json(
                200 if status_code else 500, {"ok": True, "result": payload}
            )
            return
        if cmd == "stop_stream":
            status_code, payload = _device_stream_stop(host)
            self._send_json(
                200 if status_code else 500, {"ok": True, "result": payload}
            )
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
