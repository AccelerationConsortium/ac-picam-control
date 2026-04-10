import concurrent.futures
import html
import json
import os
import socket
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

HOST = os.environ.get("PICAM_SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("PICAM_SERVER_PORT", "8081"))

RAW_DEVICES = os.environ.get(
    "PICAM_DEVICES",
    ",".join(
        [
            "cam-a.tailnet-name.ts.net",
            "cam-b.tailnet-name.ts.net",
            "cam-c.tailnet-name.ts.net",
        ]
    ),
)
DEVICE_HOSTS = [d.strip() for d in RAW_DEVICES.split(",") if d.strip()]

AGENT_PORT = int(os.environ.get("PICAM_AGENT_PORT", "8080"))
REQUEST_TIMEOUT = float(os.environ.get("PICAM_AGENT_TIMEOUT", "5"))
DEBUG = os.environ.get("PICAM_DEBUG", "0") == "1"
STATE_DB = os.environ.get("PICAM_STATE_DB", "state.db")

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


def _init_state_db():
    conn = sqlite3.connect(STATE_DB)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS desired_state (host TEXT PRIMARY KEY, should_stream INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def _set_desired_state(host, should_stream):
    conn = sqlite3.connect(STATE_DB)
    try:
        conn.execute(
            "INSERT INTO desired_state (host, should_stream, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(host) DO UPDATE SET should_stream=excluded.should_stream, updated_at=excluded.updated_at",
            (host, 1 if should_stream else 0, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _get_desired_hosts():
    conn = sqlite3.connect(STATE_DB)
    try:
        rows = conn.execute(
            "SELECT host FROM desired_state WHERE should_stream=1"
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


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


def _create_youtube_broadcast(access_token, title, stream_id):
    for status in ("active", "upcoming"):
        items = _list_broadcasts(access_token, status)
        for item in items:
            snippet = item.get("snippet") or {}
            content_details = item.get("contentDetails") or {}
            bound_stream_id = content_details.get("boundStreamId")
            if snippet.get("title") == title and item.get("id"):
                if not bound_stream_id or bound_stream_id == stream_id:
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


def _list_streams(access_token):
    _, payload = _youtube_request(
        "GET",
        "/liveStreams",
        access_token,
        params={
            "part": "snippet,cdn,contentDetails,status",
            "mine": "true",
            "maxResults": 50,
        },
        body=None,
    )
    return payload.get("items") or []


def _get_stream(access_token, stream_id):
    _, payload = _youtube_request(
        "GET",
        "/liveStreams",
        access_token,
        params={"part": "snippet,cdn,contentDetails,status", "id": stream_id},
        body=None,
    )
    items = payload.get("items") or []
    if not items:
        raise RuntimeError("Stream not found")
    return items[0]


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


def _find_stream_for_title(access_token, title):
    try:
        items = _list_streams(access_token)
    except Exception as exc:
        _log(f"stream_lookup_error error={exc}")
        return None
    for item in items:
        snippet = item.get("snippet") or {}
        if snippet.get("title") == title:
            return item
    return None


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
    existing = STREAM_STATE.get(host) or {}
    if (
        existing.get("ffmpeg_url")
        and existing.get("stream_key")
        and existing.get("broadcast_id")
    ):
        ffmpeg_url = existing["ffmpeg_url"]
        stream_key = existing["stream_key"]
        broadcast_id = existing["broadcast_id"]
        stream_id = existing.get("stream_id")
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

    broadcast = _find_broadcast_for_title(access_token, title)
    broadcast_id = broadcast.get("id") if broadcast else None
    bound_stream_id = (
        (broadcast.get("contentDetails") or {}).get("boundStreamId")
        if broadcast
        else None
    )

    stream = None
    if bound_stream_id:
        stream = _get_stream(access_token, bound_stream_id)
    elif broadcast:
        stream = _find_stream_for_title(access_token, title)
    else:
        stream = _find_stream_for_title(access_token, title)

    if stream:
        stream_id = stream["id"]
        ingestion = (stream.get("cdn") or {}).get("ingestionInfo") or {}
        ffmpeg_url = ingestion.get("ingestionAddress")
        stream_key = ingestion.get("streamName")
        if not (ffmpeg_url and stream_key):
            raise RuntimeError("YouTube stream response missing ingestion info")
        if not broadcast_id:
            broadcast_id = _create_youtube_broadcast(access_token, title, stream_id)
        elif not bound_stream_id:
            _bind_broadcast(access_token, broadcast_id, stream_id)
    else:
        stream_id, ffmpeg_url, stream_key = _create_youtube_stream(access_token, title)
        if not broadcast_id:
            broadcast_id = _create_youtube_broadcast(access_token, title, stream_id)
        else:
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
        watch_link = (
            f'<a class="watch-link" href="{html.escape(watch_url)}" target="_blank" rel="noreferrer">Watch</a>'
            if watch_url
            else '<span class="muted">—</span>'
        )
        rows.append(
            f"""
            <tr>
                <td><code>{html.escape(host)}</code></td>
                <td><span class="status {status_text}">{status_text}</span><div class="watch">{watch_link}</div></td>
                <td class="thumb">{thumb}</td>
                <td><div class="detail">{detail}</div></td>
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
    body {{ font-family: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 24px; background: linear-gradient(135deg, #ffb3c7 0%, #ffd6a5 20%, #fdffb6 40%, #caffbf 60%, #9bf6ff 80%, #bdb2ff 100%); color: #4a2c3f; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; background: rgba(255,245,250,0.92); border-radius: 16px; overflow: hidden; box-shadow: 0 12px 30px rgba(140, 70, 120, 0.12); }}
    th, td {{ border: 1px solid #f5cde6; padding: 10px; vertical-align: top; }}
    th {{ background: #ffe3f3; color: #6b2d52; }}
    .detail {{ display: block; max-width: 420px; line-height: 1.35; max-height: calc(1.35em * 8); overflow: auto; word-break: break-word; white-space: pre-wrap; background: #fff0f8; border: 1px solid #f7c8e4; border-radius: 10px; padding: 8px; }}
    .thumb img {{ width: 120px; border-radius: 10px; display: block; box-shadow: 0 8px 18px rgba(140, 70, 120, 0.18); }}
    .muted {{ color: #9b6a86; }}
    .status {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-weight: 700; text-transform: uppercase; font-size: 12px; letter-spacing: 0.4px; border: 1px solid #f3b9dc; }}
    .status.ok {{ background: #dff7e9; color: #2a6b53; }}
    .status.error {{ background: #ffd1e6; color: #8a1f3d; }}
    .watch {{ margin-top: 6px; }}
    .watch-link {{ display: inline-block; padding: 5px 10px; border-radius: 10px; background: #dbe9ff; color: #1f4db8; text-decoration: none; font-size: 12px; border: 1px solid #b7cfff; }}
    .watch-link:hover {{ background: #c7dcff; }}
    form button {{ margin-right: 6px; border-radius: 10px; border: 1px solid #f3b9dc; background: #ffe3f3; padding: 6px 10px; color: #6b2d52; }}
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
            if status_code:
                _set_desired_state(host, True)
            self._send_json(
                200 if status_code else 500, {"ok": True, "result": payload}
            )
            return
        if cmd == "stop_stream":
            status_code, payload = _device_stream_stop(host)
            if status_code:
                _set_desired_state(host, False)
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
    _init_state_db()
    for host in _get_desired_hosts():
        if host not in DEVICE_HOSTS:
            continue
        try:
            status_code, payload = _start_stream_for_host(host)
            watch_url = payload.get("watch_url")
            if watch_url:
                _log(f"resuming_stream host={host} watch_url={watch_url}")
            else:
                _log(f"resuming_stream host={host}")
        except Exception as exc:
            _log(f"resume_failed host={host} error={exc}")
    server = HTTPServer((HOST, PORT), ControlHandler)
    print(f"picam-control server listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
