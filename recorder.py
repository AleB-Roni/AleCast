"""AleCast Aufnahme-Server: nimmt Segmente der AleCorder-App entgegen,
speichert sie wie Frigate (Datum/Stunde/Kamera/MM.SS.mp4) und räumt alte Tage auf."""
import datetime as dt
import json
import os
import re
import queue
import shutil
import subprocess
import threading
import time

RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "5") or 5)
# Wird von der Web-UI gesetzt, damit die Aufbewahrung ohne Neustart änderbar ist
retain_days_provider = None


def retain_days():
    if retain_days_provider:
        try:
            return int(retain_days_provider())
        except (ValueError, TypeError):
            pass
    return RETAIN_DAYS
MAX_UPLOAD = 1024 * 1024 * 1024  # 1 GB pro Segment
INDEX = "segments.jsonl"
MOTION = "motion.jsonl"
MOTION_BUCKET_MS = 5000
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
    _jobs.put((folder, name))
    return os.path.relpath(target, RECORDINGS_DIR)


def _read_index(folder, fname=INDEX):
    entries = {}
    try:
        with open(os.path.join(folder, fname)) as f:
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
    return {"cameras": sorted(cams), "days": days, "retain_days": retain_days(),
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
        motion = _read_index(folder, MOTION)
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
            base_url = f"rec/{date}/{h}/{camera}/{name}"
            m = motion.get(name)
            out.append({"url": base_url, "start": start, "dur": dur,
                        "size": os.path.getsize(os.path.join(folder, name)),
                        "thumb": base_url[:-4] + ".jpg" if os.path.isfile(os.path.join(folder, name[:-4] + ".jpg")) else None,
                        "motion": m["m"] if m else None, "mb": m.get("bucket", MOTION_BUCKET_MS) if m else MOTION_BUCKET_MS})
    out.sort(key=lambda s: s["start"])
    return out


def resolve_file(rel):
    """Pfad unter RECORDINGS_DIR auflösen (ohne Ausbruch aus dem Ordner)."""
    base = os.path.realpath(RECORDINGS_DIR)
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep) or not full.endswith((".mp4", ".jpg")) or not os.path.isfile(full):
        return None
    return full


def cleanup_once():
    days = retain_days()
    if days <= 0:
        return
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    try:
        for d in os.listdir(RECORDINGS_DIR):
            if _date_re.match(d) and d < cutoff:
                shutil.rmtree(os.path.join(RECORDINGS_DIR, d), ignore_errors=True)
                print(f"[AleCast] Aufnahmen vom {d} gelöscht (älter als {days} Tage)", flush=True)
    except OSError:
        pass


def start_cleanup_thread():
    def loop():
        while True:
            cleanup_once()
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True).start()



# ---------- Vorschaubilder & Bewegungsanalyse (Hintergrund) ----------
_jobs = queue.Queue()
_score_re = re.compile(r"lavfi\.scene_score=([0-9.]+)")
_pts_re = re.compile(r"pts_time:([0-9.]+)")


def analyze(folder, name):
    """Erzeugt Vorschaubild (MM.SS.jpg) und Bewegungswerte je 5 s (motion.jsonl)."""
    src = os.path.join(folder, name)
    thumb = os.path.join(folder, name[:-4] + ".jpg")
    if not os.path.isfile(src):
        return
    if not os.path.isfile(thumb):
        subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", src,
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", thumb],
                       capture_output=True, timeout=60)
    if name in _read_index(folder, MOTION):
        return
    try:
        out = subprocess.run(
            ["nice", "-n", "10", "ffmpeg", "-v", "error", "-i", src, "-an", "-vf",
             "fps=5,scale=128:-2,select='gte(scene\\,0)',metadata=print:key=lavfi.scene_score:file=-",
             "-f", "null", "-"],
            capture_output=True, timeout=300, text=True).stdout
    except subprocess.TimeoutExpired:
        return
    buckets = {}
    t = 0.0
    for line in out.splitlines():
        m = _pts_re.search(line)
        if m:
            t = float(m.group(1))
            continue
        m = _score_re.search(line)
        if m and t > 0:  # erstes Bild hat keinen Vergleich
            b = int(t * 1000 // MOTION_BUCKET_MS)
            buckets[b] = max(buckets.get(b, 0.0), float(m.group(1)))
    n = (max(buckets) + 1) if buckets else 0
    values = [round(buckets.get(i, 0.0), 4) for i in range(n)]
    with _index_lock:
        with open(os.path.join(folder, MOTION), "a") as f:
            f.write(json.dumps({"file": name, "bucket": MOTION_BUCKET_MS, "m": values}) + "\n")


def _backfill():
    """Ältere Segmente ohne Vorschau/Bewegung nachträglich analysieren (neueste zuerst)."""
    try:
        for d in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
            if not _date_re.match(d):
                continue
            for h in sorted(os.listdir(os.path.join(RECORDINGS_DIR, d)), reverse=True):
                hp = os.path.join(RECORDINGS_DIR, d, h)
                if not os.path.isdir(hp):
                    continue
                for c in os.listdir(hp):
                    folder = os.path.join(hp, c)
                    if not os.path.isdir(folder):
                        continue
                    done = _read_index(folder, MOTION)
                    for name in sorted(os.listdir(folder), reverse=True):
                        if name.endswith(".mp4") and (name not in done or not os.path.isfile(os.path.join(folder, name[:-4] + ".jpg"))):
                            _jobs.put((folder, name))
    except OSError:
        pass


def start_analysis_thread():
    def worker():
        while True:
            folder, name = _jobs.get()
            try:
                analyze(folder, name)
            except Exception as e:  # noqa: BLE001 – Analyse darf den Server nie stoppen
                print(f"[AleCast] Analyse {name} fehlgeschlagen: {e}", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=_backfill, daemon=True).start()

