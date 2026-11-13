#!/usr/bin/env python3
"""
Builds timetable.json for the Geelong Line app from Transport Victoria's GTFS Schedule.

Run automatically each week by the GitHub Action in .github/workflows/timetable.yml.
Can also be run by hand:  python build_timetable.py [path/to/gtfs.zip]

Environment (optional):
  VIC_OPENDATA_KEY   your Transport Victoria Open Data key (sent as the KeyID header
                     in case the download requires it)
"""
import csv, io, json, os, re, sys, tempfile, urllib.request, zipfile
from datetime import date, datetime, timedelta, timezone

# Geelong line stations, Melbourne end first
LINE = ['Southern Cross', 'Footscray', 'Sunshine', 'Deer Park', 'Tarneit', 'West Tarneit', 'Wyndham Vale',
        'Little River', 'Lara', 'Corio', 'North Shore', 'North Geelong', 'Geelong', 'South Geelong',
        'Marshall', 'Waurn Ponds']
# A trip must call at one of these to count as a Geelong line train (filters out Ballarat/Bendigo trains)
GEELONG_ONLY = set(range(LINE.index('Tarneit'), len(LINE)))
WINDOW_BACK, WINDOW_AHEAD = 1, 42      # days of timetable to keep
CKAN = 'https://opendata.transport.vic.gov.au/api/3/action/package_show?id=gtfs-schedule'
UA = {'User-Agent': 'geelong-line-app-timetable-builder'}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def headers():
    h = dict(UA)
    if os.environ.get('VIC_OPENDATA_KEY'):
        h['KeyID'] = os.environ['VIC_OPENDATA_KEY']
    return h


def find_download_url():
    with urllib.request.urlopen(urllib.request.Request(CKAN, headers=headers()), timeout=60) as r:
        pkg = json.load(r)['result']
    zips = [res for res in pkg.get('resources', [])
            if (res.get('format') or '').lower() == 'zip' or str(res.get('url', '')).lower().endswith('.zip')]
    if not zips:
        raise SystemExit('No GTFS zip found in the gtfs-schedule dataset')
    zips.sort(key=lambda r: r.get('last_modified') or r.get('created') or '', reverse=True)
    log('Using resource:', zips[0].get('name'), zips[0].get('last_modified') or zips[0].get('created'))
    return zips[0]['url']


def download(url):
    fd, path = tempfile.mkstemp(suffix='.zip')
    log('Downloading', url)
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers()), timeout=600) as r, os.fdopen(fd, 'wb') as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    log('Downloaded', os.path.getsize(path) // (1 << 20), 'MB')
    return path


def regional_train_files(outer_path):
    """Return {name: bytes} for the Regional Train (folder 1) GTFS files."""
    outer = zipfile.ZipFile(outer_path)
    names = outer.namelist()
    nested = [n for n in names if re.search(r'(^|/)1/google_transit\.zip$', n)]
    if nested:
        inner = zipfile.ZipFile(io.BytesIO(outer.read(nested[0])))
        return {os.path.basename(n): inner.read(n) for n in inner.namelist() if n.endswith('.txt')}
    flat = [n for n in names if re.search(r'(^|/)1/[^/]+\.txt$', n)]
    if flat:
        return {os.path.basename(n): outer.read(n) for n in flat}
    if 'stops.txt' in names:   # already a single-mode GTFS
        return {n: outer.read(n) for n in names if n.endswith('.txt')}
    raise SystemExit('Could not find Regional Train (folder 1) data in the GTFS zip')


def rows(files, name):
    if name not in files:
        return []
    return list(csv.DictReader(io.StringIO(files[name].decode('utf-8-sig'))))


def clean(name):
    n = re.sub(r'\s*\(.*?\)\s*', ' ', name or '')
    n = re.sub(r'\s+(Railway\s+)?Station\b.*$', '', n, flags=re.I)
    return re.sub(r'\s+', ' ', n).strip()


def mins(t):
    if not t:
        return None
    h, m, *_ = t.split(':')
    return int(h) * 60 + int(m)


def ymd(d):
    return int(d.strftime('%Y%m%d'))


def build(files, today):
    stops = {s['stop_id']: s for s in rows(files, 'stops.txt')}
    idx_by_name = {n.lower(): i for i, n in enumerate(LINE)}

    def station_of(sid):
        s = stops.get(sid)
        if not s:
            return None
        for cand in (s, stops.get(s.get('parent_station') or '')):
            if cand:
                i = idx_by_name.get(clean(cand.get('stop_name')).lower())
                if i is not None:
                    return i
        return None

    stop_station = {sid: st for sid in stops if (st := station_of(sid)) is not None}
    platforms = {sid: (stops[sid].get('platform_code') or '').strip() for sid in stop_station
                 if (stops[sid].get('platform_code') or '').strip()}
    log('Line stations found:', sorted({LINE[i] for i in stop_station.values()}, key=LINE.index))

    routes = {r['route_id']: r for r in rows(files, 'routes.txt')}
    trips = {t['trip_id']: t for t in rows(files, 'trips.txt')}

    # service calendar -> set of active dates within the window
    start, end = today - timedelta(days=WINDOW_BACK), today + timedelta(days=WINDOW_AHEAD)
    window = [start + timedelta(days=k) for k in range((end - start).days + 1)]
    active = {}
    for c in rows(files, 'calendar.txt'):
        a, b = datetime.strptime(c['start_date'], '%Y%m%d').date(), datetime.strptime(c['end_date'], '%Y%m%d').date()
        days = [c['monday'], c['tuesday'], c['wednesday'], c['thursday'], c['friday'], c['saturday'], c['sunday']]
        active[c['service_id']] = {d for d in window if a <= d <= b and days[d.weekday()] == '1'}
    for cd in rows(files, 'calendar_dates.txt'):
        d = datetime.strptime(cd['date'], '%Y%m%d').date()
        if not (start <= d <= end):
            continue
        s = active.setdefault(cd['service_id'], set())
        (s.add if cd['exception_type'] == '1' else s.discard)(d)

    # stop times for trips on the line
    by_trip = {}
    for st in rows(files, 'stop_times.txt'):
        sta = stop_station.get(st['stop_id'])
        if sta is None:
            continue
        m = mins(st.get('departure_time')) if st.get('departure_time') else mins(st.get('arrival_time'))
        if m is None:
            continue
        by_trip.setdefault(st['trip_id'], []).append((int(st['stop_sequence']), sta, m, platforms.get(st['stop_id'], '')))

    svc_ids, out_trips = {}, []
    for tid, sts in by_trip.items():
        t = trips.get(tid)
        if not t or not active.get(t['service_id']):
            continue
        sts.sort()
        stations = [s[1] for s in sts]
        if len(set(stations)) < 2 or not (set(stations) & GEELONG_ONLY):
            continue
        route = routes.get(t['route_id'], {})
        rtype = str(route.get('route_type', '2'))
        coach = rtype not in ('2',) and not rtype.startswith('1')   # 2 / 100-117 = rail
        si = svc_ids.setdefault(t['service_id'], len(svc_ids))
        head = t.get('trip_headsign') or LINE[sts[-1][1]]
        out_trips.append([tid, si, clean(head) or head, 1 if coach else 0,
                          [[s[1], s[2], s[0], s[3]] for s in sts]])

    services = [None] * len(svc_ids)
    for sid, i in svc_ids.items():
        services[i] = sorted(ymd(d) for d in active[sid])
    out_trips.sort(key=lambda t: (t[4][0][1], t[0]))
    return {
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'from': ymd(start), 'to': ymd(end),
        'stations': LINE,
        'stopIds': stop_station,           # GTFS stop_id (incl. platforms) -> station index
        'services': services,              # list of active dates (yyyymmdd) per service
        'trips': out_trips,                # [trip_id, service, headsign, coach?, [[station, minutes, stop_sequence, platform]...]]
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else download(find_download_url())
    today = datetime.now(timezone(timedelta(hours=10))).date()
    data = build(regional_train_files(path), today)
    if not data['trips']:
        raise SystemExit('No Geelong line trips found — not updating timetable.json.')
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timetable.json')
    with open(out, 'w') as f:
        json.dump(data, f, separators=(',', ':'))
    log(f"Wrote {out}: {len(data['trips'])} trips, {len(data['services'])} services, {os.path.getsize(out)//1024} KB")


if __name__ == '__main__':
    main()
