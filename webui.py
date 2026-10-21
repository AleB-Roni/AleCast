#!/usr/bin/env python3
"""AleCast Web-UI: Handy-Bildschirm ansehen und per adb fernsteuern."""
import base64
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("WEBUI_PORT", "8080"))
PASSWORD = os.environ.get("WEBUI_PASSWORD", "")
LOG_FILE = os.environ.get("ALECAST_LOG", "/tmp/alecast.log")
PREVIEW_WIDTH = int(os.environ.get("WEBUI_PREVIEW_WIDTH", "960"))
TAPO_PACKAGE = "com.tplink.iot"

_screen_lock = threading.Lock()
_last_size = {"w": 0, "h": 0}

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
        subprocess.run(["pkill", "-f", "ffmpeg .*-f rtsp"])
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
        else:
            self._send(404, b"nicht gefunden", "text/plain")

    def do_POST(self):
        if not self._auth_ok():
            return
        if self.path != "/api/action":
            self._send(404, b"nicht gefunden", "text/plain")
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 10000)
            body = json.loads(self.rfile.read(length) or b"{}")
            ok, msg = do_action(body)
        except (ValueError, KeyError, TypeError) as e:
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
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--line);padding:8px 14px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none}
</style>
</head>
<body>
<header>
  <h1><b>Ale</b>Cast</h1>
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
    </div>
    <div class="hint">Klicken = tippen · Ziehen = wischen · Gedrückt halten = langes Tippen</div>
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

let start=null;
img.addEventListener("pointerdown",e=>{if(!dev.w)return;e.preventDefault();img.setPointerCapture(e.pointerId);start={...toDevice(e),t:Date.now()}});
img.addEventListener("pointerup",e=>{
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
refresh(); poll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print(f"[AleCast] Web-UI auf Port {PORT}" + (" (mit Passwort)" if PASSWORD else ""), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
