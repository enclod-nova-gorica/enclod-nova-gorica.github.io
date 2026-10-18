#!/usr/bin/env python3
"""
Pripravi staticne podatke za nadzorno plosco EnCLOD Nova Gorica.

Za vsak senzor iz index.html:
  - data/<id>.json : urni podatki zadnjih 7 dni (za plosco)
  - data/<id>.csv  : vsi razpolozljivi urni podatki od DATA_SINCE (za prenos)

CSV se nadgrajuje postopno: skripta prenese obstojeci CSV z objavljene strani
(PAGES_URL) in mu doda nove ure. Samo ob prvem zagonu (ali ce CSV manjka)
podatke potegne nazaj do DATA_SINCE.

Okolje:
  TELRAAM_API_KEY  Telraam API kljuc (GitHub Secret)
  PAGES_URL        naslov objavljene strani, npr. https://user.github.io/repo/
"""
import csv, io, json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

TELRAAM_URL = "https://telraam-api.net/v1/reports/traffic"
DATA_SINCE = datetime(2024, 1, 1, tzinfo=timezone.utc)
RECENT_DAYS = 7
CHUNK_DAYS = 90
SLIM_FIELDS = ("date", "uptime", "pedestrian", "bike", "car", "heavy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "_site", "data")
API_KEY = os.environ.get("TELRAAM_API_KEY", "").strip()
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/") + "/"


def sensor_ids():
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    seen, ids = set(), []
    for sid in re.findall(r'segmentId:"(\d+)"', html):
        if sid not in seen:
            seen.add(sid)
            ids.append(sid)
    return ids


def fmt(d):
    return d.strftime("%Y-%m-%d %H:%M:%S") + "Z"


def telraam(sid, start, end):
    body = json.dumps({"id": sid, "time_start": fmt(start), "time_end": fmt(end),
                       "level": "segments", "format": "per-hour"}).encode()
    req = urllib.request.Request(TELRAAM_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Api-Key": API_KEY})
    time.sleep(1.1)  # Telraam: najvec 1 zahteva na sekundo
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print(f"  [{sid}] HTTP {e.code}: {e.read()[:200]!r}")
        return None
    except Exception as e:
        print(f"  [{sid}] napaka: {e}")
        return None
    rows = j.get("report")
    return rows if isinstance(rows, list) else []


def previous_csv(sid):
    """Prebere obstojeci CSV z objavljene strani. None, ce ga ni."""
    if not PAGES_URL.startswith("http"):
        return None
    try:
        with urllib.request.urlopen(f"{PAGES_URL}data/{sid}.csv?t={int(time.time())}", timeout=30) as r:
            text = r.read().decode("utf-8-sig")
    except Exception:
        return None
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows or None


def backfill(sid, now):
    rows, end = [], now
    while end > DATA_SINCE:
        start = max(end - timedelta(days=CHUNK_DAYS), DATA_SINCE)
        chunk = telraam(sid, start, end)
        if not chunk:
            break
        rows = chunk + rows
        end = start
    return rows


def norm(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return "" if v is None else v


def write_csv(path, rows):
    cols, seen = [], set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([norm(r.get(c, "")) for c in cols])


def main():
    os.makedirs(OUT, exist_ok=True)
    if not API_KEY:
        print("TELRAAM_API_KEY ni nastavljen: stran bo objavljena z demo podatki.")
        return 0
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    ids = sensor_ids()
    status = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sensors": {}}
    for sid in ids:
        recent = telraam(sid, now - timedelta(days=RECENT_DAYS), now)
        if recent is None:
            recent = []
        old = previous_csv(sid)
        if old is None:
            print(f"  [{sid}] ni obstojecega CSV, polnim zgodovino od {DATA_SINCE.date()}")
            history = backfill(sid, now - timedelta(days=RECENT_DAYS))
        else:
            history = old
        merged = {r.get("date"): r for r in history if r.get("date")}
        for r in recent:
            if r.get("date"):
                merged[r["date"]] = r
        allrows = [merged[k] for k in sorted(merged)]
        if allrows:
            write_csv(os.path.join(OUT, f"{sid}.csv"), allrows)
        slim = [{k: r.get(k) for k in SLIM_FIELDS} for r in recent]
        with open(os.path.join(OUT, f"{sid}.json"), "w", encoding="utf-8") as f:
            json.dump({"segmentId": sid, "generated": status["generated"], "rows": slim}, f, separators=(",", ":"))
        status["sensors"][sid] = {"recent_rows": len(recent), "csv_rows": len(allrows)}
        print(f"  [{sid}] zadnjih {RECENT_DAYS} dni: {len(recent)} ur, CSV skupaj: {len(allrows)} ur")
    with open(os.path.join(OUT, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
