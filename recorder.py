"""AleCast Aufnahme-Server: nimmt Segmente der AleCorder-App entgegen,
speichert sie wie Frigate (Datum/Stunde/Kamera/MM.SS.mp4) und räumt alte Tage auf."""
import datetime as dt
import json
import os
import re
import shutil
import threading
import time

RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "5"))
MAX_UPLOAD = 1024 * 1024 * 1024  # 1 GB pro Segment
INDEX = "segments.jsonl"
_index_lock = threading.Lock()
_cam_re = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def safe_camera(name):
    name = (name or "").strip()
    if not _cam_re.match(name):
        raise ValueError("ungültiger Kameraname (erlaubt: A-Z, 0-9, _ und -)")
    return name


def store_segment(rfile, length, camera, start_ms, dur_ms):
    """Liest den Upload in eine Datei und trägt ihn in den Stunden-Index ein."""
    camera = safe_camera(camera)
    if length <= 0 or length > MAX_UPLOAD:
        raise ValueError("ungültige Dateigröße")
    start_ms = int(start_ms)
    dur_ms = max(0, int(dur_ms))
    t = dt.datetime.fromtimestamp(start_ms / 1000)
    folder = os.path.join(RECORDINGS_DIR, t.strftime("%Y-%m-%d"), t.strftime("%H"), camera)
    os.makedirs(folder, exist_ok=True)
    name = t.strftime("%M.%S") + ".mp4"
    target = os.path.join(folder, name)
    tmp = target + ".part"
    remaining = length
    with open(tmp, "wb") as f:
        while remaining > 0:
            chunk = rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Upload abgebrochen")
            f.write(chunk)
            remaining -= len(chunk)
    os.replace(tmp, target)
    with _index_lock:
        with open(os.path.join(folder, INDEX), "a") as f:
            f.write(json.dumps({"file": name, "start": start_ms, "dur": dur_ms}) + "\n")
    return os.path.relpath(target, RECORDINGS_DIR)


def _read_index(folder):
    entries = {}
    try:
        with open(os.path.join(folder, INDEX)) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    entries[e["file"]] = e  # Duplikate (Wiederholungen) zusammenfassen
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return entries


def list_overview():
    """Kameras und vorhandene Tage."""
    cams, days = set(), []
    try:
        for d in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
            if not _date_re.match(d):
                continue
            day_path = os.path.join(RECORDINGS_DIR, d)
            found = False
            for h in os.listdir(day_path):
                hp = os.path.join(day_path, h)
                if os.path.isdir(hp):
                    for c in os.listdir(hp):
                        if os.path.isdir(os.path.join(hp, c)):
                            cams.add(c)
                            found = True
            if found:
                days.append(d)
    except OSError:
        pass
    total, used, free = (0, 0, 0)
    try:
        total, used, free = shutil.disk_usage(RECORDINGS_DIR)
    except OSError:
        pass
    return {"cameras": sorted(cams), "days": days, "retain_days": RETAIN_DAYS,
            "disk_free_gb": round(free / 1e9, 1), "disk_total_gb": round(total / 1e9, 1)}


def list_segments(camera, date):
    camera = safe_camera(camera)
    if not _date_re.match(date or ""):
        raise ValueError("ungültiges Datum")
    out = []
    day_path = os.path.join(RECORDINGS_DIR, date)
    for h in sorted(os.listdir(day_path)) if os.path.isdir(day_path) else []:
        folder = os.path.join(day_path, h, camera)
        if not os.path.isdir(folder):
            continue
        idx = _read_index(folder)
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".mp4"):
                continue
            e = idx.get(name)
            if e:
                start, dur = e["start"], e["dur"]
            else:  # Datei ohne Index: Zeit aus dem Pfad
                mm, ss = name[:-4].split(".")
                start = int(dt.datetime.strptime(f"{date} {h}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                dur = 60000
            out.append({"url": f"rec/{date}/{h}/{camera}/{name}", "start": start, "dur": dur,
                        "size": os.path.getsize(os.path.join(folder, name))})
    out.sort(key=lambda s: s["start"])
    return out


def resolve_file(rel):
    """Pfad unter RECORDINGS_DIR auflösen (ohne Ausbruch aus dem Ordner)."""
    base = os.path.realpath(RECORDINGS_DIR)
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep) or not full.endswith(".mp4") or not os.path.isfile(full):
        return None
    return full


def cleanup_once():
    if RETAIN_DAYS <= 0:
        return
    cutoff = (dt.date.today() - dt.timedelta(days=RETAIN_DAYS)).isoformat()
    try:
        for d in os.listdir(RECORDINGS_DIR):
            if _date_re.match(d) and d < cutoff:
                shutil.rmtree(os.path.join(RECORDINGS_DIR, d), ignore_errors=True)
                print(f"[AleCast] Aufnahmen vom {d} gelöscht (älter als {RETAIN_DAYS} Tage)", flush=True)
    except OSError:
        pass


def start_cleanup_thread():
    def loop():
        while True:
            cleanup_once()
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True).start()


REC_PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AleCast Aufnahmen</title>
<style>
:root{--bg:#111315;--panel:#1a1d20;--line:#2a2e33;--text:#e6e8ea;--muted:#8b9299;--accent:#ff8a3d;--rec:#3a7bd5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:17px;margin:0}h1 b{color:var(--accent)}
a{color:var(--accent);text-decoration:none}
select,button{background:#23272b;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 10px;font:inherit;cursor:pointer}
button:hover{border-color:var(--accent)}
main{max-width:1200px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:14px}
video{width:100%;aspect-ratio:16/9;max-height:65vh;background:#000;border:1px solid var(--line);border-radius:10px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.muted{color:var(--muted);font-size:12px}
.tl{position:relative;height:46px;background:var(--panel);border:1px solid var(--line);border-radius:8px;cursor:pointer;overflow:hidden}
.tl .seg{position:absolute;top:8px;bottom:18px;background:var(--rec);opacity:.85}
.tl .seg.cur{background:var(--accent);opacity:1}
.tl .tick{position:absolute;bottom:2px;font-size:10px;color:var(--muted);transform:translateX(-50%)}
.tl .line{position:absolute;top:0;bottom:14px;width:1px;background:var(--line)}
.tl .pos{position:absolute;top:0;bottom:0;width:2px;background:#fff}
.time{font-variant-numeric:tabular-nums;font-size:20px;font-weight:600}
.empty{padding:40px;text-align:center;color:var(--muted);background:var(--panel);border:1px dashed var(--line);border-radius:10px}
</style>
</head>
<body>
<header>
  <h1><b>Ale</b>Cast · Aufnahmen</h1>
  <select id="cam"></select>
  <select id="day"></select>
  <span class="muted" id="disk"></span>
  <a href="./" style="margin-left:auto">← Steuerung</a>
</header>
<main>
  <div id="empty" class="empty" style="display:none">Noch keine Aufnahmen. Sobald AleCorder Segmente hochlädt, erscheinen sie hier.</div>
  <video id="v" controls playsinline muted></video>
  <div class="bar">
    <span class="time" id="clock">--:--:--</span>
    <button id="prev">◀ Segment</button>
    <button id="next">Segment ▶</button>
    <select id="speed"><option value="1">1×</option><option value="2">2×</option><option value="4">4×</option><option value="8">8×</option></select>
    <a id="dl" href="#" download>Segment herunterladen</a>
    <span class="muted" id="info" style="margin-left:auto"></span>
  </div>
  <div class="tl" id="tl"></div>
  <div class="muted">Auf die Zeitleiste klicken, um zu einer Uhrzeit zu springen. Blau = aufgenommen.</div>
</main>
<script>
const $=id=>document.getElementById(id), v=$("v");
let segs=[], cur=-1, dayStart=0;
const pad=n=>String(n).padStart(2,"0");
const fmt=ms=>{const d=new Date(ms);return pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds())};
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(await r.text());return r.json()}
async function init(){
  const o=await j("api/recordings");
  $("disk").textContent=`Aufbewahrung ${o.retain_days} Tage · ${o.disk_free_gb} GB frei`;
  if(!o.cameras.length){$("empty").style.display="block";return}
  $("cam").innerHTML=o.cameras.map(c=>`<option>${c}</option>`).join("");
  $("day").innerHTML=o.days.map(d=>`<option>${d}</option>`).join("");
  loadDay();
}
async function loadDay(){
  segs=await j(`api/recordings?camera=${encodeURIComponent($("cam").value)}&date=${$("day").value}`);
  const [y,m,d]=$("day").value.split("-").map(Number); dayStart=new Date(y,m-1,d).getTime();
  drawTl(); cur=-1;
  if(segs.length) play(segs.length-1,0);
  $("info").textContent=segs.length+" Segmente";
}
function drawTl(){
  const tl=$("tl"), day=86400000; let h="";
  for(let i=0;i<=24;i+=3){const x=i/24*100;h+=`<div class="line" style="left:${x}%"></div><div class="tick" style="left:${Math.min(97,Math.max(2,x))}%">${pad(i)}:00</div>`}
  segs.forEach((s,i)=>{const l=(s.start-dayStart)/day*100,w=Math.max(.08,s.dur/day*100);h+=`<div class="seg" data-i="${i}" style="left:${l}%;width:${w}%"></div>`});
  h+=`<div class="pos" id="pos" style="display:none"></div>`; tl.innerHTML=h;
}
function play(i,offset){
  if(i<0||i>=segs.length)return; cur=i; const s=segs[i];
  v.src=s.url; $("dl").href=s.url;
  v.onloadedmetadata=()=>{v.currentTime=Math.min(offset||0,Math.max(0,v.duration-0.5));v.playbackRate=+$("speed").value;v.play().catch(()=>{})};
  document.querySelectorAll(".seg").forEach(e=>e.classList.toggle("cur",+e.dataset.i===i));
}
v.addEventListener("ended",()=>{if(cur+1<segs.length)play(cur+1,0)});
v.addEventListener("timeupdate",()=>{
  if(cur<0)return; const t=segs[cur].start+v.currentTime*1000; $("clock").textContent=fmt(t);
  const p=$("pos"); p.style.display="block"; p.style.left=((t-dayStart)/86400000*100)+"%";
});
$("tl").addEventListener("click",e=>{
  const b=$("tl").getBoundingClientRect(), t=dayStart+(e.clientX-b.left)/b.width*86400000;
  let best=-1,bd=Infinity;
  segs.forEach((s,i)=>{if(t>=s.start&&t<=s.start+s.dur){best=i;bd=0}else{const d=Math.min(Math.abs(t-s.start),Math.abs(t-s.start-s.dur));if(bd>0&&d<bd){bd=d;best=i}}});
  if(best>=0)play(best,Math.max(0,(t-segs[best].start)/1000));
});
$("prev").onclick=()=>play(cur-1,0); $("next").onclick=()=>play(cur+1,0);
$("speed").onchange=()=>v.playbackRate=+$("speed").value;
$("cam").onchange=loadDay; $("day").onchange=loadDay;
init().catch(e=>{$("empty").textContent="Fehler: "+e.message;$("empty").style.display="block"});
</script>
</body>
</html>
"""
