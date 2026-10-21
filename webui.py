#!/usr/bin/env python3
"""AleCast Web-UI: Handy-Bildschirm ansehen und per adb fernsteuern."""
import base64
import json
import os
import subprocess
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recorder  # noqa: E402
import live  # noqa: E402

PORT = int(os.environ.get("WEBUI_PORT", "8080"))
PASSWORD = os.environ.get("WEBUI_PASSWORD", "")
LOG_FILE = os.environ.get("ALECAST_LOG", "/tmp/alecast.log")
PREVIEW_WIDTH = int(os.environ.get("WEBUI_PREVIEW_WIDTH", "960"))
TAPO_PACKAGE = "com.tplink.iot"

SETTINGS_FILE = os.environ.get("ALECAST_SETTINGS", "/root/.android/alecast-settings.json")
OUT_WIDTHS = (0, 640, 960, 1280, 1920)
SOURCE_SIZES = (0, 1280, 1600, 1920, 4096)  # 0 = Standard (MAX_SIZE), 4096 = volle Auflösung

_screen_lock = threading.Lock()
_last_size = {"w": 0, "h": 0}


ENV_RTSP_URL = os.environ.get("RTSP_URL", "").strip()
ENV_RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "5") or 5)


def default_settings():
    # streaming/retain_days = None bedeutet: Wert aus den Container-Variablen verwenden
    return {"crop": {"enabled": False, "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "out_width": 0, "source_size": 0,
            "streaming": None, "rtsp_url": "", "retain_days": None}


def effective(s):
    """Tatsächlich gültige Werte (Web-UI-Einstellung, sonst Container-Variable)."""
    url = s.get("rtsp_url") or ENV_RTSP_URL
    streaming = s.get("streaming")
    if streaming is None:
        streaming = bool(ENV_RTSP_URL)
    days = s.get("retain_days")
    if days is None:
        days = ENV_RETAIN_DAYS
    return {"streaming": bool(streaming) and bool(url), "streaming_wanted": bool(streaming),
            "rtsp_url": url, "retain_days": int(days)}


def load_settings():
    s = default_settings()
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        crop = data.get("crop", {})
        for k in ("x", "y", "w", "h"):
            if k in crop:
                s["crop"][k] = float(crop[k])
        s["crop"]["enabled"] = bool(crop.get("enabled", False))
        if int(data.get("out_width", 0)) in OUT_WIDTHS:
            s["out_width"] = int(data["out_width"])
        if int(data.get("source_size", 0)) in SOURCE_SIZES:
            s["source_size"] = int(data["source_size"])
        if data.get("streaming") is not None:
            s["streaming"] = bool(data["streaming"])
        s["rtsp_url"] = str(data.get("rtsp_url") or "")
        if data.get("retain_days") is not None:
            s["retain_days"] = int(data["retain_days"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return s


def sanitize_crop(c):
    x = min(max(float(c.get("x", 0)), 0.0), 0.99)
    y = min(max(float(c.get("y", 0)), 0.0), 0.99)
    w = min(max(float(c.get("w", 1)), 0.01), 1.0 - x)
    h = min(max(float(c.get("h", 1)), 0.01), 1.0 - y)
    full = x < 0.001 and y < 0.001 and w > 0.999 and h > 0.999
    return {"enabled": bool(c.get("enabled", True)) and not full,
            "x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def save_settings(body):
    s = load_settings()
    if "crop" in body:
        s["crop"] = sanitize_crop(body["crop"])
    if "out_width" in body:
        ow = int(body["out_width"])
        if ow not in OUT_WIDTHS:
            raise ValueError("ungültige Ausgabebreite")
        s["out_width"] = ow
    if "source_size" in body:
        ss = int(body["source_size"])
        if ss not in SOURCE_SIZES:
            raise ValueError("ungültige Quellauflösung")
        s["source_size"] = ss
    if "streaming" in body:
        s["streaming"] = bool(body["streaming"])
    if "rtsp_url" in body:
        url = str(body["rtsp_url"] or "").strip()
        if url and not url.startswith(("rtsp://", "rtsps://")):
            raise ValueError("RTSP-Ziel muss mit rtsp:// beginnen")
        s["rtsp_url"] = url
    if "retain_days" in body:
        days = int(body["retain_days"])
        if not 0 <= days <= 3650:
            raise ValueError("Aufbewahrung muss zwischen 0 und 3650 Tagen liegen")
        s["retain_days"] = days
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)
    return s


def ffmpeg_filter(s):
    """Zusätzliche ffmpeg-Filter für Ausschnitt und Skalierung (leer = keine)."""
    parts = []
    c = s["crop"]
    if c["enabled"]:
        parts.append("crop=trunc(iw*{w}/2)*2:trunc(ih*{h}/2)*2:trunc(iw*{x}):trunc(ih*{y})".format(**c))
    if s["out_width"]:
        parts.append(f"scale={s['out_width']}:-2:flags=lanczos")
    return ",".join(parts)


def restart_stream():
    subprocess.run(["pkill", "-f", "ffmpeg .*-f rtsp"])

KEYS = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "wake": "KEYCODE_WAKEUP",
    "power": "KEYCODE_POWER",
    "vol_up": "KEYCODE_VOLUME_UP",
    "vol_down": "KEYCODE_VOLUME_DOWN",
    "enter": "KEYCODE_ENTER",
    "del": "KEYCODE_DEL",
}


def adb(*args, timeout=10, binary=False):
    try:
        out = subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
        return out.stdout if binary else out.stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError):
        return b"" if binary else ""


def png_size(data):
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def capture_screen():
    """Screenshot als JPEG (verkleinert) plus echte Bildschirmgröße."""
    with _screen_lock:
        png = adb("exec-out", "screencap", "-p", timeout=8, binary=True)
    w, h = png_size(png)
    if not w:
        return None, 0, 0
    _last_size.update(w=w, h=h)
    try:
        jpg = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "png_pipe", "-i", "-",
             "-vf", f"scale='min({PREVIEW_WIDTH},iw)':-2", "-q:v", "6",
             "-f", "mjpeg", "-"],
            input=png, capture_output=True, timeout=8).stdout
    except (subprocess.TimeoutExpired, OSError):
        jpg = b""
    if not jpg:
        return png, w, h  # Fallback: PNG
    return jpg, w, h


def tail_log(lines=40):
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16000))
            text = f.read().decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError:
        return ""


def status():
    state = adb("get-state", timeout=4).strip()
    info = {"connected": state == "device", "state": state or "kein Gerät"}
    if info["connected"]:
        info["model"] = adb("shell", "getprop", "ro.product.model", timeout=4).strip()
        bat = adb("shell", "dumpsys", "battery", timeout=4)
        for line in bat.splitlines():
            line = line.strip()
            if line.startswith("level:"):
                info["battery"] = line.split(":", 1)[1].strip()
            elif line.startswith("temperature:"):
                try:
                    info["temp"] = int(line.split(":", 1)[1]) / 10
                except ValueError:
                    pass
        focus = adb("shell", "dumpsys", "window", timeout=4)
        info["tapo_front"] = TAPO_PACKAGE in next(
            (l for l in focus.splitlines() if "mCurrentFocus" in l), "")
    streaming = subprocess.run(["pgrep", "-f", "ffmpeg .*-f rtsp"],
                               capture_output=True).returncode == 0
    info["streaming"] = streaming
    info["log"] = tail_log()
    return info


def clamp_int(v, lo=0, hi=10000):
    return max(lo, min(hi, int(float(v))))


def do_action(body):
    kind = body.get("type")
    if kind == "tap":
        adb("shell", "input", "tap", str(clamp_int(body["x"])), str(clamp_int(body["y"])))
    elif kind == "swipe":
        adb("shell", "input", "swipe",
            *(str(clamp_int(body[k])) for k in ("x1", "y1", "x2", "y2")),
            str(clamp_int(body.get("ms", 300), 50, 5000)))
    elif kind == "key":
        code = KEYS.get(body.get("key"))
        if not code:
            return False, "unbekannte Taste"
        adb("shell", "input", "keyevent", code)
    elif kind == "text":
        text = str(body.get("text", ""))[:500]
        # adb input text: Leerzeichen als %s, Sonderzeichen escapen
        safe = "".join(c if c.isalnum() else ("%s" if c == " " else "\\" + c) for c in text)
        adb("shell", "input", "text", safe)
    elif kind == "center":
        w, h = _last_size["w"], _last_size["h"]
        if not w:
            _, w, h = capture_screen()
        if not w:
            return False, "Bildschirmgröße unbekannt"
        adb("shell", "input", "tap", str(w // 2), str(h // 2))
    elif kind == "unlock":
        adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        w, h = _last_size["w"] or 1080, _last_size["h"] or 2000
        adb("shell", "input", "swipe", str(w // 2), str(int(h * 0.8)),
            str(w // 2), str(int(h * 0.2)), "300")
    elif kind == "tapo":
        adb("shell", "monkey", "-p", TAPO_PACKAGE, "-c",
            "android.intent.category.LAUNCHER", "1", timeout=15)
    elif kind == "tapo_restart":
        adb("shell", "am", "force-stop", TAPO_PACKAGE)
        adb("shell", "monkey", "-p", TAPO_PACKAGE, "-c",
            "android.intent.category.LAUNCHER", "1", timeout=15)
    elif kind == "rotate":
        mode = "1" if body.get("landscape", True) else "0"
        adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
        adb("shell", "settings", "put", "system", "user_rotation", mode)
    elif kind == "stream_restart":
        restart_stream()
    else:
        return False, "unbekannte Aktion"
    return True, "ok"


class Handler(BaseHTTPRequestHandler):
    server_version = "AleCast"

    def log_message(self, *args):
        pass

    def _auth_ok(self):
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user_pw = base64.b64decode(header[6:]).decode()
                if user_pw.split(":", 1)[-1] == PASSWORD:
                    return True
            except ValueError:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AleCast"')
        self.end_headers()
        return False

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith(PUBLIC_PATHS):
            body, ctype = static_file(path)
            if body is None:
                self._send(404, b"nicht gefunden", "text/plain")
            else:
                extra = {"Service-Worker-Allowed": "/"} if path == "/sw.js" else None
                self._send(200, body, ctype, extra)
            return
        if not self._auth_ok():
            return
        if path in ("/", "/index.html", "/recordings", "/history"):
            self._send(200, app_page().encode(), "text/html; charset=utf-8")
        elif path == "/screen":
            img, w, h = capture_screen()
            if not img:
                self._send(503, b"kein Bild", "text/plain")
                return
            ctype = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
            self._send(200, img, ctype, {"X-Device-W": str(w), "X-Device-H": str(h)})
        elif path == "/api/status":
            self._json(status())
        elif path == "/api/settings":
            cur = load_settings()
            self._json({**cur, "effective": effective(cur)})
        elif path == "/api/recordings":
            q = parse_qs(urlparse(self.path).query)
            try:
                if "camera" in q and "date" in q:
                    self._json(recorder.list_segments(q["camera"][0], q["date"][0]))
                else:
                    self._json(recorder.list_overview())
            except (ValueError, OSError) as e:
                self._send(400, str(e).encode(), "text/plain; charset=utf-8")
        elif path == "/api/live":
            self._json(live.stream.status())
        elif path == "/live.mjpg":
            self._live_mjpeg()
        elif path == "/live.jpg":
            frame = live.stream.latest()
            if frame:
                self._send(200, frame, "image/jpeg")
            else:
                self._send(503, (live.stream.error or "kein Live-Bild").encode(), "text/plain; charset=utf-8")
        elif path.startswith("/rec/"):
            self._send_file(unquote(path[len("/rec/"):]))
        else:
            self._send(404, b"nicht gefunden", "text/plain")

    def _send_file(self, rel):
        full = recorder.resolve_file(rel)
        if not full:
            self._send(404, b"nicht gefunden", "text/plain")
            return
        size = os.path.getsize(full)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        partial = bool(m) and (m.group(1) or m.group(2))
        if partial:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(512 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _live_mjpeg(self):
        if not live.device_connected():
            self._send(503, "Handy nicht per USB an AleCast verbunden".encode(), "text/plain; charset=utf-8")
            return
        live.stream.acquire()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=alecastframe")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in live.stream.frames():
                self.wfile.write(b"--alecastframe\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            live.stream.release()

    def _upload(self):
        q = parse_qs(urlparse(self.path).query)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            rel = recorder.store_segment(self.rfile, length, q.get("camera", [""])[0],
                                         q.get("start", ["0"])[0], q.get("duration", ["0"])[0])
            self._json({"ok": True, "file": rel})
        except (ValueError, OSError) as e:
            self._json({"ok": False, "msg": str(e)}, 400)

    def do_POST(self):
        if not self._auth_ok():
            return
        if self.path.startswith("/api/upload"):
            self._upload()
            return
        if self.path not in ("/api/action", "/api/settings"):
            self._send(404, b"nicht gefunden", "text/plain")
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 10000)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/settings":
                before = load_settings()
                s = save_settings(body)
                stream_keys = ("crop", "out_width", "source_size", "streaming", "rtsp_url")
                if any(before.get(k) != s.get(k) for k in stream_keys) or before["rtsp_url"] != s["rtsp_url"]:
                    restart_stream()
                if before.get("retain_days") != s.get("retain_days"):
                    threading.Thread(target=recorder.cleanup_once, daemon=True).start()
                self._json({"ok": True, "msg": "gespeichert", "settings": s, "effective": effective(s)})
                return
            ok, msg = do_action(body)
        except (ValueError, KeyError, TypeError, OSError) as e:
            ok, msg = False, f"ungültige Anfrage: {e}"
        self._json({"ok": ok, "msg": msg}, 200 if ok else 400)


_APP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html")
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_STATIC_TYPES = {".webmanifest": "application/manifest+json", ".js": "text/javascript; charset=utf-8",
                 ".png": "image/png", ".svg": "image/svg+xml"}
# Für die App-Installation: Browser laden Manifest und Symbole ohne Anmeldedaten
PUBLIC_PATHS = ("/manifest.webmanifest", "/sw.js", "/icons/")


def static_file(path):
    rel = path.lstrip("/")
    full = os.path.realpath(os.path.join(_STATIC_DIR, rel))
    if not full.startswith(os.path.realpath(_STATIC_DIR) + os.sep) or not os.path.isfile(full):
        return None, None
    ext = os.path.splitext(full)[1]
    with open(full, "rb") as f:
        return f.read(), _STATIC_TYPES.get(ext, "application/octet-stream")


def app_page():
    with open(_APP_PATH, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cur = load_settings()
        if sys.argv[1] == "--get-vf":
            print(ffmpeg_filter(cur))
        elif sys.argv[1] == "--get-max-size":
            print(cur["source_size"] or "")
        elif sys.argv[1] == "--get-streaming":
            print("1" if effective(cur)["streaming"] else "0")
        elif sys.argv[1] == "--get-rtsp-url":
            print(effective(cur)["rtsp_url"])
        sys.exit(0)
    print(f"[AleCast] Web-UI auf Port {PORT}" + (" (mit Passwort)" if PASSWORD else ""), flush=True)
    recorder.retain_days_provider = lambda: effective(load_settings())["retain_days"]
    recorder.start_cleanup_thread()
    recorder.start_analysis_thread()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
