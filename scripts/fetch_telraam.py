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
import csv, io, json, math, os, re, sys, time, urllib.parse, urllib.request, urllib.error
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


# ---------------------------------------------------------------- zemljevidi
MAP_VERSION = 1
MAP_W, MAP_H = 400, 260          # viewBox v px
MAP_HALF_W_M = 380               # pol sirine izreza v metrih
MAX_NEW_MAPS = 4                 # najvec novih zemljevidov na zagon (da objava ostane hitra)
UA = "EnCLOD-NovaGorica-dashboard/1.0 (University of Ljubljana, Faculty of Architecture)"
HW_CLASS = {
    "motorway": "major", "trunk": "major", "primary": "major",
    "motorway_link": "major", "trunk_link": "major", "primary_link": "major",
    "secondary": "mid", "tertiary": "mid", "secondary_link": "mid", "tertiary_link": "mid",
    "residential": "minor", "unclassified": "minor", "living_street": "minor",
    "pedestrian": "minor", "road": "minor",
    "service": "service",
}


def sensor_addresses():
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    return {sid: addr for addr, sid in re.findall(r'address:"([^"]+)", segmentId:"(\d+)"', html)}


def http_json(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def segment_lines(sid):
    """Geometrija segmenta iz Telraama: seznam crt [[lon,lat],...]."""
    try:
        j = http_json(f"https://telraam-api.net/v1/segments/id/{sid}", headers={"X-Api-Key": API_KEY})
        time.sleep(1.1)
        g = (j.get("features") or [{}])[0].get("geometry") or {}
        if g.get("type") == "LineString":
            return [g["coordinates"]]
        if g.get("type") == "MultiLineString":
            return g["coordinates"]
    except Exception as e:
        print(f"  [{sid}] segment ni na voljo: {e}")
    return []


def geocode(addr):
    try:
        q = urllib.parse.urlencode({"format": "json", "limit": 1, "q": addr})
        j = http_json(f"https://nominatim.openstreetmap.org/search?{q}")
        time.sleep(1.2)
        if j:
            return float(j[0]["lon"]), float(j[0]["lat"])
    except Exception as e:
        print(f"  geokodiranje ni uspelo ({addr}): {e}")
    return None


def overpass_ways(lon, lat, radius):
    q = (f'[out:json][timeout:40];way(around:{radius},{lat},{lon})'
         f'[highway~"^({"|".join(HW_CLASS)})$"];out geom;')
    for url in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        try:
            j = http_json(url, data=urllib.parse.urlencode({"data": q}).encode(), timeout=90)
            time.sleep(2)
            return j.get("elements", [])
        except Exception as e:
            print(f"  Overpass {url}: {e}")
            time.sleep(5)
    return None


def build_map(sid, addr):
    lines = segment_lines(sid)
    pts = [p for ln in lines for p in ln]
    if pts:
        lon0 = sum(p[0] for p in pts) / len(pts)
        lat0 = sum(p[1] for p in pts) / len(pts)
    else:
        c = geocode(addr)
        if not c:
            return None
        lon0, lat0 = c
    ways = overpass_ways(lon0, lat0, int(MAP_HALF_W_M * 1.25))
    if ways is None:
        return None
    kx = math.cos(math.radians(lat0)) * 111320.0
    ky = 110540.0
    s = MAP_W / (2.0 * MAP_HALF_W_M)

    def xy(lon, lat):
        return (MAP_W / 2 + (lon - lon0) * kx * s, MAP_H / 2 - (lat - lat0) * ky * s)

    def path(coords):
        out, last = [], None
        for lon, lat in coords:
            x, y = xy(lon, lat)
            if last and abs(x - last[0]) < 1.2 and abs(y - last[1]) < 1.2:
                continue
            out.append((x, y))
            last = (x, y)
        if len(out) < 2:
            return ""
        margin = 40
        if all(x < -margin or x > MAP_W + margin or y < -margin or y > MAP_H + margin for x, y in out):
            return ""
        return "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in out)

    streets = {}
    for w in ways:
        cls = HW_CLASS.get((w.get("tags") or {}).get("highway"))
        geom = w.get("geometry")
        if not cls or not geom:
            continue
        d = path([(g["lon"], g["lat"]) for g in geom])
        if d:
            streets.setdefault(cls, []).append(d)
    seg = "".join(path(ln) for ln in lines)
    return {"v": MAP_VERSION, "w": MAP_W, "h": MAP_H, "seg": seg, "pt": [MAP_W / 2, MAP_H / 2],
            "streets": {k: "".join(v) for k, v in streets.items()},
            "attribution": "© OpenStreetMap contributors"}


def ensure_maps(ids):
    addrs = sensor_addresses()
    built = 0
    for sid in ids:
        path_out = os.path.join(OUT, f"map_{sid}.json")
        prev = None
        if PAGES_URL.startswith("http"):
            try:
                prev = http_json(f"{PAGES_URL}data/map_{sid}.json?t={int(time.time())}", timeout=30)
            except Exception:
                prev = None
        if prev and prev.get("v") == MAP_VERSION:
            m = prev
        elif built >= MAX_NEW_MAPS:
            m = None
        else:
            built += 1
            m = build_map(sid, addrs.get(sid, "Nova Gorica"))
            print(f"  [{sid}] zemljevid {'pripravljen' if m else 'NI pripravljen'}")
        if m:
            with open(path_out, "w", encoding="utf-8") as f:
                json.dump(m, f, separators=(",", ":"))


def main():
    os.makedirs(OUT, exist_ok=True)
    if not API_KEY:
        print("TELRAAM_API_KEY ni nastavljen: stran bo objavljena z demo podatki.")
        return 0
    try:
        ensure_maps(sensor_ids())
    except Exception as e:
        print(f"Zemljevidi: napaka {e}")
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
