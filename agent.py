import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

SERVICE_NAME = os.environ.get("PICAM_SERVICE_NAME", "picam")
HOST = os.environ.get("PICAM_AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("PICAM_AGENT_PORT", "8080"))
DEBUG = os.environ.get("PICAM_DEBUG", "0") == "1"
PICAM_SERVER_URL = os.environ.get("PICAM_SERVER_URL", "")
PICAM_POLL_INTERVAL = float(os.environ.get("PICAM_POLL_INTERVAL", "8"))
PICAM_HOSTNAME = os.environ.get("PICAM_HOSTNAME", socket.gethostname())
PICAM_VFLIP = os.environ.get("PICAM_VFLIP", "1") == "1"

STREAM_WIDTH = int(os.environ.get("PICAM_WIDTH", "640"))
STREAM_HEIGHT = int(os.environ.get("PICAM_HEIGHT", "360"))
STREAM_FPS = int(os.environ.get("PICAM_FPS", "15"))
VIDEO_BITRATE = os.environ.get("PICAM_VIDEO_BITRATE", "1000k")
VIDEO_MAXRATE = os.environ.get("PICAM_VIDEO_MAXRATE", VIDEO_BITRATE)
VIDEO_BUFSIZE = os.environ.get("PICAM_VIDEO_BUFSIZE", "2000k")
X264_PRESET = os.environ.get("PICAM_X264_PRESET", "ultrafast")
INPUT_CODEC = os.environ.get("PICAM_INPUT_CODEC", "raw").lower()
CAMERA_BITRATE = os.environ.get("PICAM_CAMERA_BITRATE", "1200000")

JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
}

STREAM_CAMERA_PROC = None
STREAM_FFMPEG_PROC = None


def _log(message):
    if DEBUG:
        print(message, flush=True)


def _run_systemctl(*args):
    _log(f"systemctl args={args} service={SERVICE_NAME}")
    result = subprocess.run(
        ["systemctl", *args, SERVICE_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    _log(f"systemctl exit_code={result.returncode}")
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


def _get_camera_command():
    if shutil.which("rpicam-vid"):
        return "rpicam-vid"
    if shutil.which("libcamera-vid"):
        return "libcamera-vid"
    raise RuntimeError("Neither 'rpicam-vid' nor 'libcamera-vid' command found")


def _stream_status():
    return {
        "ok": STREAM_FFMPEG_PROC is not None,
        "running": STREAM_FFMPEG_PROC is not None,
    }


def _stop_stream():
    global STREAM_CAMERA_PROC, STREAM_FFMPEG_PROC
    if STREAM_FFMPEG_PROC:
        STREAM_FFMPEG_PROC.terminate()
    if STREAM_CAMERA_PROC:
        STREAM_CAMERA_PROC.terminate()
    STREAM_FFMPEG_PROC = None
    STREAM_CAMERA_PROC = None


def _fetch_desired_state():
    if not PICAM_SERVER_URL:
        return None
    url = f"{PICAM_SERVER_URL.rstrip('/')}/desired-state/{PICAM_HOSTNAME}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log(f"desired_state_error host={PICAM_HOSTNAME} error={exc}")
        return None


def _reconcile_loop():
    while True:
        try:
            desired = _fetch_desired_state()
            if desired and desired.get("ok"):
                should_stream = bool(desired.get("should_stream"))
                if should_stream and STREAM_FFMPEG_PROC is None:
                    ffmpeg_url = desired.get("ffmpeg_url")
                    stream_key = desired.get("stream_key")
                    if ffmpeg_url and stream_key:
                        _log(f"reconcile_start host={PICAM_HOSTNAME}")
                        _start_stream(ffmpeg_url, stream_key)
                    else:
                        _log("reconcile_missing_stream_info")
                if not should_stream and STREAM_FFMPEG_PROC is not None:
                    _log(f"reconcile_stop host={PICAM_HOSTNAME}")
                    _stop_stream()
        except Exception as exc:
            _log(f"reconcile_error error={exc}")
        time.sleep(PICAM_POLL_INTERVAL)


def _start_stream(ffmpeg_url, stream_key):
    global STREAM_CAMERA_PROC, STREAM_FFMPEG_PROC
    _stop_stream()

    gop = str(STREAM_FPS * 2)
    camera_cmd = _get_camera_command()
    libcamera_cmd = [
        camera_cmd,
        "--inline",
        "--nopreview",
        "-t",
        "0",
        "--mode",
        "1280:720",
        "--width",
        str(STREAM_WIDTH),
        "--height",
        str(STREAM_HEIGHT),
        "--framerate",
        str(STREAM_FPS),
    ]
    if PICAM_VFLIP:
        libcamera_cmd.append("--vflip")
    if INPUT_CODEC == "h264":
        libcamera_cmd.extend(
            [
                "--codec",
                "h264",
                "--profile",
                "baseline",
                "--intra",
                gop,
                "--bitrate",
                str(CAMERA_BITRATE),
            ]
        )
    else:
        libcamera_cmd.extend(["--codec", "yuv420"])

    libcamera_cmd.extend(["-o", "-"])

    if INPUT_CODEC == "h264":
        input_opts = [
            "-f",
            "h264",
            "-r",
            str(STREAM_FPS),
            "-i",
            "pipe:0",
        ]
    else:
        input_opts = [
            "-f",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            "-s",
            f"{STREAM_WIDTH}x{STREAM_HEIGHT}",
            "-r",
            str(STREAM_FPS),
            "-i",
            "pipe:0",
        ]

    ffmpeg_cmd = (
        [
            "ffmpeg",
            "-thread_queue_size",
            "1024",
            "-use_wallclock_as_timestamps",
            "1",
            "-fflags",
            "+genpts",
            "-analyzeduration",
            "10000000",
            "-probesize",
            "10000000",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        + input_opts
        + [
            "-filter:a",
            "aresample=async=1:first_pts=0",
            "-c:v",
            "libx264",
            "-preset",
            X264_PRESET,
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            gop,
            "-keyint_min",
            gop,
            "-sc_threshold",
            "0",
            "-b:v",
            VIDEO_BITRATE,
            "-maxrate",
            VIDEO_MAXRATE,
            "-bufsize",
            VIDEO_BUFSIZE,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-f",
            "flv",
            f"{ffmpeg_url.rstrip('/')}/{stream_key}",
        ]
    )

    STREAM_CAMERA_PROC = subprocess.Popen(
        libcamera_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    STREAM_FFMPEG_PROC = subprocess.Popen(
        ffmpeg_cmd, stdin=STREAM_CAMERA_PROC.stdout, stderr=subprocess.STDOUT
    )
    STREAM_CAMERA_PROC.stdout.close()


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
        _log(f"http_get path={path}")
        if path == "/health":
            self._send_json(200, {"ok": True, "service": SERVICE_NAME})
            return
        if path == "/status":
            payload = _get_status()
            self._send_json(200 if payload["ok"] else 500, payload)
            return
        if path == "/stream/status":
            payload = _stream_status()
            self._send_json(200 if payload["ok"] else 503, payload)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        _log(f"http_post path={path}")
        if path == "/stream/start":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "Invalid JSON"})
                return
            ffmpeg_url = payload.get("ffmpeg_url")
            stream_key = payload.get("stream_key")
            if not ffmpeg_url or not stream_key:
                self._send_json(
                    400, {"ok": False, "error": "ffmpeg_url and stream_key required"}
                )
                return
            try:
                _start_stream(ffmpeg_url, stream_key)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/stream/stop":
            _stop_stream()
            self._send_json(200, {"ok": True})
            return
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
    _log(f"agent_start hostname={PICAM_HOSTNAME} server_url={PICAM_SERVER_URL}")
    if PICAM_SERVER_URL:
        thread = threading.Thread(target=_reconcile_loop, daemon=True)
        thread.start()
    server = HTTPServer((HOST, PORT), PicamHandler)
    print(f"picam-control listening on {HOST}:{PORT} for service '{SERVICE_NAME}'")
    server.serve_forever()


if __name__ == "__main__":
    main()
