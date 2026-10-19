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
from zoneinfo import ZoneInfo

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
MAP_VERSION = 2
MAP_W, MAP_H = 400, 260          # viewBox v px
MAP_HALF_W_M = 380               # pol sirine izreza v metrih
MAX_NEW_MAPS = 8                 # najvec novih zemljevidov na zagon (da objava ostane hitra)
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
    hw = "|".join(HW_CLASS)
    q = (f'[out:json][timeout:60];('
         f'way(around:{radius},{lat},{lon})[highway~"^({hw})$"];'
         f'way(around:{radius},{lat},{lon})[building];'
         f');out geom;')
    for url in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        try:
            j = http_json(url, data=urllib.parse.urlencode({"data": q}).encode(), timeout=120)
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
    ways = overpass_ways(lon0, lat0, int(MAP_HALF_W_M * 1.3))
    if ways is None:
        return None
    kx = math.cos(math.radians(lat0)) * 111320.0
    ky = 110540.0
    s = MAP_W / (2.0 * MAP_HALF_W_M)
    margin = 30

    def xy(lon, lat):
        return (MAP_W / 2 + (lon - lon0) * kx * s, MAP_H / 2 - (lat - lat0) * ky * s)

    def simplify(coords, tol=1.2):
        out, last = [], None
        for lon, lat in coords:
            x, y = xy(lon, lat)
            if last and abs(x - last[0]) < tol and abs(y - last[1]) < tol:
                continue
            out.append((x, y))
            last = (x, y)
        return out

    def visible(pp):
        xs = [p[0] for p in pp]; ys = [p[1] for p in pp]
        return not (max(xs) < -margin or min(xs) > MAP_W + margin or max(ys) < -margin or min(ys) > MAP_H + margin)

    def d_line(pp, nd=1):
        return "M" + "L".join(f"{x:.{nd}f} {y:.{nd}f}" for x, y in pp)

    streets, buildings, named = {}, [], {}
    for w in ways:
        tags = w.get("tags") or {}
        geom = w.get("geometry")
        if not geom:
            continue
        coords = [(g["lon"], g["lat"]) for g in geom]
        if "building" in tags and "highway" not in tags:
            pp = simplify(coords, 0.8)
            if len(pp) < 3 or not visible(pp):
                continue
            xs = [p[0] for p in pp]; ys = [p[1] for p in pp]
            if (max(xs) - min(xs)) < 1.5 and (max(ys) - min(ys)) < 1.5:
                continue
            buildings.append(d_line(pp, 0) + "Z")
            continue
        cls = HW_CLASS.get(tags.get("highway"))
        if not cls:
            continue
        pp = simplify(coords)
        if len(pp) < 2 or not visible(pp):
            continue
        streets.setdefault(cls, []).append(d_line(pp))
        name = tags.get("name")
        if name and cls in ("major", "mid", "minor"):
            named.setdefault(name, []).append((cls, pp))

    # imena ulic: najdaljsi vidni odsek vsake ulice, najvec 5 napisov, brez prekrivanja
    rank = {"major": 0, "mid": 1, "minor": 2}
    cands = []
    for name, parts in named.items():
        best = None
        for cls, pp in parts:
            for (x1, y1), (x2, y2) in zip(pp, pp[1:]):
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                if not (25 < mx < MAP_W - 25 and 14 < my < MAP_H - 14):
                    continue
                ln = math.hypot(x2 - x1, y2 - y1)
                if best is None or ln > best[0]:
                    best = (ln, mx, my, math.degrees(math.atan2(y2 - y1, x2 - x1)), cls)
        if best and best[0] > 18:
            cands.append((rank[best[4]], -best[0], name, best))
    cands.sort()
    labels = []
    cx, cy = MAP_W / 2, MAP_H / 2
    for _, _, name, (ln, mx, my, ang, cls) in cands:
        if math.hypot(mx - cx, my - cy) < 22:
            continue
        if any(math.hypot(mx - l["x"], my - l["y"]) < 70 for l in labels):
            continue
        if ang > 90: ang -= 180
        if ang < -90: ang += 180
        labels.append({"t": name, "x": round(mx, 1), "y": round(my, 1), "a": round(ang, 1)})
        if len(labels) >= 5:
            break

    seg = "".join(d_line(pp) for pp in (simplify(ln) for ln in lines) if len(pp) >= 2)
    return {"v": MAP_VERSION, "w": MAP_W, "h": MAP_H, "seg": seg, "pt": [MAP_W / 2, MAP_H / 2],
            "streets": {k: "".join(v) for k, v in streets.items()},
            "buildings": "".join(buildings), "labels": labels,
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
        m = None
        if prev and prev.get("v") == MAP_VERSION:
            m = prev
        elif built < MAX_NEW_MAPS:
            built += 1
            m = build_map(sid, addrs.get(sid, "Nova Gorica"))
            print(f"  [{sid}] zemljevid {'pripravljen' if m else 'NI pripravljen'}")
        if not m and prev:
            m = prev                       # starejsa razlicica, dokler nova ni narejena
        if m:
            with open(path_out, "w", encoding="utf-8") as f:
                json.dump(m, f, separators=(",", ":"))


# ---------------------------------------------------------------- dnevni povzetki in povprecja
LJ = ZoneInfo("Europe/Ljubljana")
METRIC_KEYS = ("pedestrian", "bike", "car", "heavy")
PROFILE_WEEKS = 8


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_daily(sid, rows):
    """Dnevne vsote (lokalni cas) in povprecni urni profil po dnevih v tednu."""
    days = {}
    parsed = []
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        try:
            t = datetime.fromisoformat(str(d).replace("Z", "+00:00")).astimezone(LJ)
        except ValueError:
            continue
        up = fnum(r.get("uptime"))
        vals = [fnum(r.get(k)) for k in METRIC_KEYS]
        parsed.append((t, up, vals))
        key = t.strftime("%Y-%m-%d")
        rec = days.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0])
        for i, v in enumerate(vals):
            rec[i] += v
        if up > 0:
            rec[4] += 1
    day_list = [[k] + [round(v) for v in rec[:4]] + [rec[4]] for k, rec in sorted(days.items())]
    # urni profil: zadnjih PROFILE_WEEKS tednov, samo ure z delujocim senzorjem
    prof = {}
    if parsed:
        last = max(p[0] for p in parsed)
        since = last - timedelta(weeks=PROFILE_WEEKS)
        for t, up, vals in parsed:
            if t < since or up <= 0.3:
                continue
            k = f"{t.weekday()}-{t.hour}"
            acc = prof.setdefault(k, [0.0, 0.0, 0.0, 0.0, 0])
            for i, v in enumerate(vals):
                acc[i] += v
            acc[4] += 1
    profile = {k: [round(a[i] / a[4], 1) for i in range(4)] + [a[4]] for k, a in prof.items() if a[4] >= 2}
    with open(os.path.join(OUT, f"{sid}_daily.json"), "w", encoding="utf-8") as f:
        json.dump({"segmentId": sid, "days": day_list, "profile": profile, "weeks": PROFILE_WEEKS},
                  f, separators=(",", ":"))


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
            try:
                build_daily(sid, allrows)
            except Exception as e:
                print(f"  [{sid}] dnevni povzetek: napaka {e}")
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
