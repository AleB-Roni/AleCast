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
        if not self._auth_ok():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
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
        elif path == "/recordings":
            self._send(200, recorder.REC_PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/recordings":
            q = parse_qs(urlparse(self.path).query)
            try:
                if "camera" in q and "date" in q:
                    self._json(recorder.list_segments(q["camera"][0], q["date"][0]))
                else:
                    self._json(recorder.list_overview())
            except (ValueError, OSError) as e:
                self._send(400, str(e).encode(), "text/plain; charset=utf-8")
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


PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AleCast</title>
<style>
:root{--bg:#111315;--panel:#1a1d20;--line:#2a2e33;--text:#e6e8ea;--muted:#8b9299;--accent:#ff8a3d;--ok:#3ecf8e;--bad:#ef5f5f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:17px;margin:0;letter-spacing:.3px}
h1 b{color:var(--accent)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;background:var(--panel);border:1px solid var(--line);color:var(--muted);font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
main{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;padding:16px;max-width:1500px;margin:0 auto}
@media (max-width:900px){main{grid-template-columns:1fr}}
.screen{position:relative;background:#000;border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;align-items:center;justify-content:center;min-height:200px;touch-action:none;user-select:none}
.screen img{max-width:100%;max-height:78vh;display:block;cursor:crosshair;-webkit-user-drag:none}
.screen .msg{position:absolute;color:var(--muted)}
.ripple{position:absolute;width:28px;height:28px;margin:-14px 0 0 -14px;border-radius:50%;border:2px solid var(--accent);pointer-events:none;animation:r .5s ease-out forwards}
@keyframes r{from{transform:scale(.4);opacity:1}to{transform:scale(1.6);opacity:0}}
.hint{color:var(--muted);font-size:12px;margin-top:6px}
aside{display:flex;flex-direction:column;gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.grid2{grid-template-columns:repeat(2,1fr)}
button{background:#23272b;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:9px 6px;font:inherit;cursor:pointer}
button:hover{border-color:var(--accent)}
button:active{transform:translateY(1px)}
button.primary{background:var(--accent);color:#1a1006;border-color:var(--accent);font-weight:600}
.row{display:flex;gap:6px}
select{background:#0d0f11;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 6px;font:inherit}
input[type=text]{flex:1;min-width:0;background:#0d0f11;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:0;font-size:13px}
dt{color:var(--muted)}dd{margin:0;text-align:right}
pre{margin:0;max-height:220px;overflow:auto;font:11px/1.45 ui-monospace,Menlo,Consolas,monospace;color:#b9c0c6;white-space:pre-wrap;word-break:break-all}
label.sw{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;margin-left:auto}
.crop{position:absolute;border:2px solid var(--accent);box-shadow:0 0 0 9999px rgba(0,0,0,.5);pointer-events:none;display:none}
.crop.draft{border-style:dashed}
.crop span{position:absolute;left:0;top:-22px;background:var(--accent);color:#1a1006;font-size:11px;font-weight:600;padding:2px 6px;border-radius:4px 4px 0 0;white-space:nowrap}
.screen.cropmode img{cursor:cell}
button.on{background:var(--accent);color:#1a1006;border-color:var(--accent)}
.nums{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:8px 0}
.nums label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--muted)}
.nums input{width:100%;background:#0d0f11;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px;font:inherit}
.sel{display:grid;grid-template-columns:auto 1fr;gap:6px 10px;align-items:center;font-size:12px;color:var(--muted);margin-bottom:8px}
.sel select{width:100%;padding:6px}
.note{font-size:11px;color:var(--muted);margin-top:8px}
.tog{display:flex;align-items:center;gap:8px;font-size:14px;margin-bottom:10px;cursor:pointer}
.tog input{width:18px;height:18px;accent-color:var(--accent)}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:10px;font-size:12px;color:var(--muted)}
.field input{width:100%;background:#0d0f11;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px;font:inherit}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--line);padding:8px 14px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none}
</style>
</head>
<body>
<header>
  <h1><b>Ale</b>Cast</h1>
  <a href="recordings" class="pill" style="text-decoration:none;color:var(--accent)">▶ Aufnahmen</a>
  <span class="pill"><span class="dot" id="dDev"></span><span id="tDev">Handy …</span></span>
  <span class="pill"><span class="dot" id="dStr"></span><span id="tStr">Stream …</span></span>
  <label class="sw"><input type="checkbox" id="live" checked> Live-Bild</label>
  <label class="sw" style="margin-left:0">
    <select id="rate"><option value="500">0,5 s</option><option value="1000" selected>1 s</option><option value="3000">3 s</option></select>
  </label>
</header>
<main>
  <section>
    <div class="screen" id="screen">
      <span class="msg" id="msg">Lade Bildschirm …</span>
      <img id="img" alt="Handy-Bildschirm" draggable="false">
      <div class="crop" id="crop"><span id="cropLabel"></span></div>
    </div>
    <div class="hint" id="hint">Klicken = tippen · Ziehen = wischen · Gedrückt halten = langes Tippen</div>
  </section>
  <aside>
    <div class="card">
      <h2>Navigation</h2>
      <div class="grid">
        <button data-key="back">◁ Zurück</button>
        <button data-key="home">○ Home</button>
        <button data-key="recents">▢ Apps</button>
        <button data-act="unlock">Entsperren</button>
        <button data-key="wake">Wecken</button>
        <button data-key="power">Power</button>
      </div>
    </div>
    <div class="card">
      <h2>Tapo &amp; Stream</h2>
      <div class="grid grid2">
        <button class="primary" data-act="center">Mitte tippen</button>
        <button data-act="tapo">Tapo öffnen</button>
        <button data-act="tapo_restart">Tapo neu starten</button>
        <button data-act="stream_restart">Stream neu starten</button>
        <button data-rot="1">Querformat</button>
        <button data-rot="0">Hochformat</button>
      </div>
    </div>
    <div class="card" id="settings">
      <h2>Streaming &amp; Aufbewahrung</h2>
      <label class="tog"><input type="checkbox" id="stOn"> RTSP-Streaming an Frigate</label>
      <div class="field"><span>RTSP-Ziel</span><input type="text" id="stUrl" placeholder="rtsp://192.168.178.108:8554/tapo_app"></div>
      <div class="field"><span>Aufnahmen behalten (Tage, 0 = unbegrenzt)</span><input type="number" id="retDays" min="0" max="3650"></div>
      <button class="primary" id="stSave" style="width:100%">Speichern</button>
      <div class="note" id="stInfo"></div>
    </div>
    <div class="card">
      <h2>Ausschnitt (RTSP-Stream)</h2>
      <div class="grid grid2">
        <button id="cropMode">▭ Bereich ziehen</button>
        <button id="cropFull">Ganzer Bildschirm</button>
      </div>
      <div class="grid" style="margin-top:6px">
        <button data-preset="0,0,.5,1">Links ½</button>
        <button data-preset=".25,0,.5,1">Mitte ½</button>
        <button data-preset=".5,0,.5,1">Rechts ½</button>
        <button data-preset="0,0,1,.5">Oben ½</button>
        <button data-preset=".25,.25,.5,.5">Zentrum</button>
        <button data-preset="0,.5,1,.5">Unten ½</button>
      </div>
      <div class="nums">
        <label>X %<input type="number" id="cx" min="0" max="99" step="0.5"></label>
        <label>Y %<input type="number" id="cy" min="0" max="99" step="0.5"></label>
        <label>Breite %<input type="number" id="cw" min="1" max="100" step="0.5"></label>
        <label>Höhe %<input type="number" id="ch" min="1" max="100" step="0.5"></label>
      </div>
      <div class="sel">
        <span>Quellauflösung</span>
        <select id="srcSize">
          <option value="0">Standard</option><option value="1280">1280</option>
          <option value="1600">1600</option><option value="1920">1920</option>
          <option value="4096">Voll (Handy-Auflösung)</option>
        </select>
        <span>Ausgabebreite</span>
        <select id="outW">
          <option value="0">wie Ausschnitt</option><option value="640">640 px</option>
          <option value="960">960 px</option><option value="1280">1280 px</option>
          <option value="1920">1920 px</option>
        </select>
      </div>
      <div class="grid grid2">
        <button class="primary" id="cropApply">Übernehmen</button>
        <button id="cropReset">Verwerfen</button>
      </div>
      <div class="note" id="cropInfo">Aktiv: ganzer Bildschirm</div>
    </div>
    <div class="card">
      <h2>Text eingeben</h2>
      <div class="row">
        <input type="text" id="text" placeholder="Text ans Handy senden">
        <button id="send">Senden</button>
      </div>
      <div class="grid grid2" style="margin-top:6px">
        <button data-key="del">⌫ Löschen</button>
        <button data-key="enter">↵ Enter</button>
      </div>
    </div>
    <div class="card">
      <h2>Status</h2>
      <dl>
        <dt>Gerät</dt><dd id="sModel">–</dd>
        <dt>Akku</dt><dd id="sBat">–</dd>
        <dt>Tapo im Vordergrund</dt><dd id="sTapo">–</dd>
      </dl>
    </div>
    <div class="card">
      <h2>Log</h2>
      <pre id="log"></pre>
    </div>
  </aside>
</main>
<div id="toast"></div>
<script>
const $=id=>document.getElementById(id);
const img=$("img"), screen=$("screen");
let dev={w:0,h:0}, busy=false, timer=null;

function toast(t){const e=$("toast");e.textContent=t;e.style.opacity=1;clearTimeout(e._t);e._t=setTimeout(()=>e.style.opacity=0,1500)}

async function act(body,label){
  try{
    const r=await fetch("api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const j=await r.json(); toast(j.ok?(label||"OK"):("Fehler: "+j.msg));
  }catch(e){toast("Keine Verbindung")}
  setTimeout(refresh,250);
}

async function refresh(){
  if(busy) return; busy=true;
  try{
    const r=await fetch("screen?t="+Date.now());
    if(!r.ok) throw 0;
    dev.w=+r.headers.get("X-Device-W"); dev.h=+r.headers.get("X-Device-H");
    const url=URL.createObjectURL(await r.blob());
    img.onload=()=>URL.revokeObjectURL(url);
    img.src=url; $("msg").style.display="none";
  }catch(e){$("msg").textContent="Kein Bild – Handy verbunden?";$("msg").style.display="block"}
  busy=false; schedule();
}
function schedule(){clearTimeout(timer); if($("live").checked) timer=setTimeout(refresh,+$("rate").value)}
$("live").onchange=schedule; $("rate").onchange=schedule;

function toDevice(ev){
  const b=img.getBoundingClientRect();
  const x=(ev.clientX-b.left)/b.width, y=(ev.clientY-b.top)/b.height;
  return {x:Math.round(x*dev.w), y:Math.round(y*dev.h), px:ev.clientX-screen.getBoundingClientRect().left, py:ev.clientY-screen.getBoundingClientRect().top};
}
function ripple(p){const d=document.createElement("div");d.className="ripple";d.style.left=p.px+"px";d.style.top=p.py+"px";screen.appendChild(d);setTimeout(()=>d.remove(),600)}

/* ---------- Aufnahmebereich ---------- */
let saved={crop:{enabled:false,x:0,y:0,w:1,h:1},out_width:0,source_size:0};
let draft=null, cropMode=false, cstart=null;
const r4=v=>Math.round(v*10000)/10000;
function frac(ev){const b=img.getBoundingClientRect();
  return {x:Math.min(1,Math.max(0,(ev.clientX-b.left)/b.width)), y:Math.min(1,Math.max(0,(ev.clientY-b.top)/b.height))}}
function isFull(c){return c.x<0.001&&c.y<0.001&&c.w>0.999&&c.h>0.999}
function drawCrop(){
  const el=$("crop"), c=draft||saved.crop;
  if(!img.naturalWidth||(!draft&&(!c.enabled||isFull(c)))){el.style.display="none";return}
  const b=img.getBoundingClientRect(), s=screen.getBoundingClientRect();
  el.style.display="block"; el.classList.toggle("draft",!!draft);
  el.style.left=(b.left-s.left+c.x*b.width)+"px"; el.style.top=(b.top-s.top+c.y*b.height)+"px";
  el.style.width=(c.w*b.width)+"px"; el.style.height=(c.h*b.height)+"px";
  const pw=dev.w?Math.round(c.w*dev.w):0, ph=dev.h?Math.round(c.h*dev.h):0;
  $("cropLabel").textContent=(draft?"Entwurf ":"")+(pw?pw+"×"+ph+" px":"");
}
function fillInputs(c){$("cx").value=+(c.x*100).toFixed(1);$("cy").value=+(c.y*100).toFixed(1);$("cw").value=+(c.w*100).toFixed(1);$("ch").value=+(c.h*100).toFixed(1)}
function setDraft(c){
  c.x=Math.min(.99,Math.max(0,c.x)); c.y=Math.min(.99,Math.max(0,c.y));
  c.w=Math.min(1-c.x,Math.max(.01,c.w)); c.h=Math.min(1-c.y,Math.max(.01,c.h));
  draft={x:r4(c.x),y:r4(c.y),w:r4(c.w),h:r4(c.h)}; fillInputs(draft); drawCrop();
}
function infoText(){
  const c=saved.crop, parts=[];
  parts.push(c.enabled&&!isFull(c)?`Ausschnitt X ${+(c.x*100).toFixed(1)} %, Y ${+(c.y*100).toFixed(1)} %, ${+(c.w*100).toFixed(1)} × ${+(c.h*100).toFixed(1)} %`:"ganzer Bildschirm");
  if(saved.out_width) parts.push("Ausgabe "+saved.out_width+" px breit");
  if(saved.source_size) parts.push("Quelle "+(saved.source_size>=4096?"voll":saved.source_size));
  $("cropInfo").textContent="Aktiv: "+parts.join(" · ");
}
async function loadSettings(){
  try{saved=await (await fetch("api/settings")).json()}catch(e){}
  draft=null; fillInputs(saved.crop); $("outW").value=saved.out_width; $("srcSize").value=saved.source_size; infoText(); drawCrop();
}
function setCropMode(on){cropMode=on;$("cropMode").classList.toggle("on",on);screen.classList.toggle("cropmode",on);
  $("hint").textContent=on?"Rechteck über den gewünschten Bereich ziehen, dann „Übernehmen“":"Klicken = tippen · Ziehen = wischen · Gedrückt halten = langes Tippen"}
$("cropMode").onclick=()=>setCropMode(!cropMode);
$("cropFull").onclick=()=>setDraft({x:0,y:0,w:1,h:1});
document.querySelectorAll("[data-preset]").forEach(b=>b.onclick=()=>{const [x,y,w,h]=b.dataset.preset.split(",").map(Number);setDraft({x,y,w,h})});
["cx","cy","cw","ch"].forEach(id=>$(id).onchange=()=>setDraft({x:+$("cx").value/100,y:+$("cy").value/100,w:+$("cw").value/100,h:+$("ch").value/100}));
$("cropReset").onclick=()=>{setCropMode(false);loadSettings()};
$("cropApply").onclick=async()=>{
  const c=draft||saved.crop;
  const body={crop:{enabled:!isFull(c),x:c.x,y:c.y,w:c.w,h:c.h},out_width:+$("outW").value,source_size:+$("srcSize").value};
  try{
    const r=await fetch("api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const j=await r.json(); toast(j.ok?"Übernommen – Stream startet neu":"Fehler: "+j.msg);
  }catch(e){toast("Keine Verbindung")}
  setCropMode(false); loadSettings();
};
window.addEventListener("resize",drawCrop);
img.addEventListener("load",drawCrop);

let start=null;
img.addEventListener("pointerdown",e=>{if(!dev.w)return;e.preventDefault();img.setPointerCapture(e.pointerId);
  if(cropMode){cstart=frac(e);setDraft({x:cstart.x,y:cstart.y,w:.01,h:.01});return}
  start={...toDevice(e),t:Date.now()}});
img.addEventListener("pointermove",e=>{if(!cropMode||!cstart)return;const p=frac(e);
  setDraft({x:Math.min(cstart.x,p.x),y:Math.min(cstart.y,p.y),w:Math.abs(p.x-cstart.x),h:Math.abs(p.y-cstart.y)})});
img.addEventListener("pointerup",e=>{
  if(cropMode){cstart=null;return}
  if(!start)return; const end=toDevice(e), ms=Date.now()-start.t;
  const dist=Math.hypot(end.x-start.x,end.y-start.y);
  ripple(start);
  if(dist<20 && ms<500) act({type:"tap",x:start.x,y:start.y},"Getippt");
  else if(dist<20) act({type:"swipe",x1:start.x,y1:start.y,x2:start.x,y2:start.y,ms:Math.min(ms,3000)},"Lang getippt");
  else act({type:"swipe",x1:start.x,y1:start.y,x2:end.x,y2:end.y,ms:Math.max(150,Math.min(ms,1500))},"Gewischt");
  start=null;
});

document.querySelectorAll("[data-key]").forEach(b=>b.onclick=()=>act({type:"key",key:b.dataset.key},b.textContent.trim()));
document.querySelectorAll("[data-act]").forEach(b=>b.onclick=()=>act({type:b.dataset.act},b.textContent.trim()));
document.querySelectorAll("[data-rot]").forEach(b=>b.onclick=()=>act({type:"rotate",landscape:b.dataset.rot==="1"},b.textContent.trim()));
function sendText(){const t=$("text").value;if(!t)return;act({type:"text",text:t},"Text gesendet");$("text").value=""}
$("send").onclick=sendText; $("text").onkeydown=e=>{if(e.key==="Enter")sendText()};

async function poll(){
  try{
    const s=await (await fetch("api/status")).json();
    $("dDev").className="dot "+(s.connected?"ok":"bad"); $("tDev").textContent=s.connected?(s.model||"Handy"):"Handy: "+s.state;
    $("dStr").className="dot "+(s.streaming?"ok":"bad"); $("tStr").textContent=s.streaming?"Stream läuft":"Stream aus";
    $("sModel").textContent=s.model||"–";
    $("sBat").textContent=s.battery?(s.battery+" %"+(s.temp?" · "+s.temp+" °C":"")):"–";
    $("sTapo").textContent=s.connected?(s.tapo_front?"ja":"nein"):"–";
    const log=$("log"), atEnd=log.scrollTop+log.clientHeight>=log.scrollHeight-5;
    log.textContent=s.log||""; if(atEnd) log.scrollTop=log.scrollHeight;
  }catch(e){}
  setTimeout(poll,5000);
}

/* ---------- Streaming & Aufbewahrung ---------- */
function stInfo(e){
  const parts=[];
  if(e.streaming) parts.push("Streaming an → "+e.rtsp_url);
  else if(e.streaming_wanted&&!e.rtsp_url) parts.push("Streaming an, aber kein RTSP-Ziel eingetragen");
  else parts.push("Streaming aus – nur AleCorder-Aufnahmen");
  parts.push(e.retain_days>0?("Aufnahmen werden nach "+e.retain_days+" Tagen gelöscht"):"Aufnahmen werden nie gelöscht");
  $("stInfo").textContent=parts.join(" · ");
}
async function loadStream(){
  try{const s=await (await fetch("api/settings")).json(), e=s.effective;
    $("stOn").checked=e.streaming_wanted; $("stUrl").value=e.rtsp_url; $("retDays").value=e.retain_days; stInfo(e);
  }catch(err){}
}
$("stSave").onclick=async()=>{
  const body={streaming:$("stOn").checked,rtsp_url:$("stUrl").value.trim(),retain_days:parseInt($("retDays").value||"0",10)};
  try{
    const r=await fetch("api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const j=await r.json();
    if(j.ok){stInfo(j.effective);toast(j.effective.streaming?"Gespeichert – Stream startet":"Gespeichert")}else toast("Fehler: "+j.msg);
  }catch(e){toast("Keine Verbindung")}
};
refresh(); poll(); loadSettings(); loadStream();
</script>
</body>
</html>
"""

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
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
