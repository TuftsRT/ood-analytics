"""
OnDemand Usage: a read-only Passenger (WSGI) app for Open OnDemand that
reports logins, sessions, and app usage from the database maintained by
ood-usage-collect. Standard library only; Python 3.6+.

Routes (relative to the app root, e.g. /pun/sys/usage-stats/):
  /                    dashboard page
  /api/summary         JSON  ?start=YYYY-MM-DD&end=YYYY-MM-DD&group=day|week|month|year
  /api/series.csv      CSV of the time series for the same parameters
  /api/users.csv       CSV of per-user totals for the same date range
  /issues              issues page (same template, issues view)
  /api/issues          JSON  errors, response times, and web server starts

Wall, core, and GPU hours come from the collector's Slurm accounting pass
and are reported as hours rounded to 0.1.
"""

import calendar
import csv
import datetime
import html
import io
import json
import os
import sqlite3
import time
from urllib.parse import parse_qs



CONFIG_PATH = os.environ.get("OOD_USAGE_CONFIG", "/etc/ood/config/ood_usage.json")

HERE = os.path.dirname(os.path.abspath(__file__))
UNKNOWN_USER = "(unknown)"

HOURS = "ROUND(SUM(CASE WHEN category = '{0}' THEN count ELSE 0 END) / 3600.0, 1)"
WALL_HOURS, CORE_HOURS, GPU_HOURS = (HOURS.format(c) for c in ("walltime", "core_time", "gpu_time"))

PERIOD_SQL = {
    "day": "day",
    "week": "date(day, '-6 days', 'weekday 0')",  # Sunday that starts the week
    "month": "substr(day, 1, 7)",
    "year": "substr(day, 1, 4)",
}


class HTTPError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def load_config():
    cfg = {"db_path": "/var/lib/ood-usage/usage.db", "show_top_users": True,
           "site_name": "Open OnDemand", "walltime": "auto", "slow_request_seconds": 3}
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
        for key in cfg:
            if key in data:
                cfg[key] = data[key]
    except (OSError, ValueError):
        pass
    return cfg


def connect(db_path):
    if not os.path.exists(db_path):
        raise HTTPError("503 Service Unavailable",
                        "No usage database at %s. Run ood-usage-collect once as root to create it." % db_path)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        conn.execute("SELECT 1 FROM usage LIMIT 1")
    except sqlite3.Error as exc:
        raise HTTPError("503 Service Unavailable",
                        "Cannot read %s (%s). Check that your account is in the database's group." % (db_path, exc))
    conn.row_factory = sqlite3.Row
    return conn


def parse_date(value, name):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPError("400 Bad Request", "%s must be a date in YYYY-MM-DD form" % name)


def period_key(d, group):
    if group == "day":
        return d.isoformat()
    if group == "week":
        return (d - datetime.timedelta(days=(d.weekday() + 1) % 7)).isoformat()
    if group == "year":
        return d.strftime("%Y")
    return d.strftime("%Y-%m")


def all_periods(start, end, group):
    keys, seen, d = [], set(), start
    while d <= end:
        k = period_key(d, group)
        if k not in seen:
            seen.add(k)
            keys.append(k)
        d += datetime.timedelta(days=1)
    return keys


def read_params(conn, environ):
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    get = lambda k: qs.get(k, [None])[0]
    lo, hi = conn.execute("SELECT min(day), max(day) FROM usage").fetchone()
    today = datetime.date.today()
    end = parse_date(get("end"), "end") if get("end") else today
    start = parse_date(get("start"), "start") if get("start") else end - datetime.timedelta(days=89)
    if start > end:
        raise HTTPError("400 Bad Request", "start must be on or before end")
    if (end - start).days > 366 * 25:
        raise HTTPError("400 Bad Request", "date range is too large")
    group = get("group") or "week"
    if group not in PERIOD_SQL:
        raise HTTPError("400 Bad Request", "group must be day, week, month, or year")
    return start, end, group, lo, hi


def series(conn, start, end, group):
    rows = conn.execute("""
        SELECT {p} AS period,
               SUM(CASE WHEN category = 'login'   THEN count ELSE 0 END) AS logins,
               SUM(CASE WHEN category = 'session' THEN count ELSE 0 END) AS sessions,
               COUNT(DISTINCT CASE WHEN category = 'active' THEN user END) AS active_users,
               SUM(CASE WHEN category = 'launch'  THEN count ELSE 0 END) AS launches,
               {wall} AS wall_hours,
               {core} AS core_hours,
               {gpu} AS gpu_hours
        FROM usage WHERE day BETWEEN ? AND ?
        GROUP BY period""".format(p=PERIOD_SQL[group], wall=WALL_HOURS, core=CORE_HOURS, gpu=GPU_HOURS),
        (start.isoformat(), end.isoformat())).fetchall()
    by_period = {r["period"]: dict(r) for r in rows}
    empty = {"logins": 0, "sessions": 0, "active_users": 0, "launches": 0,
             "wall_hours": 0, "core_hours": 0, "gpu_hours": 0}
    out = []
    for key in all_periods(start, end, group):
        row = dict(empty, period=key)
        row.update(by_period.get(key, {}))
        out.append(row)
    return out


def utc_hour_epoch(item):
    """'YYYY-MM-DDTHH' (UTC) -> epoch seconds."""
    return calendar.timegm((int(item[0:4]), int(item[5:7]), int(item[8:10]), int(item[11:13]), 0, 0))


def local_midnight(d):
    return int(time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1)))


def server_timezone():
    """IANA name of this host's time zone when it can be found, else its abbreviation."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if not tz:
        try:
            tz = os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
        except IndexError:
            tz = ""
    return {"name": tz or time.strftime("%Z"), "abbr": time.strftime("%Z")}


def heatmap(conn, start, end):
    """Distinct active users per UTC hour, for the browser to bin by its own
    day of week and hour of day.

    `span` is the first and last hour to average over: the selected range,
    trimmed to the collected hourly history so days before it don't dilute
    the averages while quiet days inside it still count.
    """
    rng = (start.isoformat(), end.isoformat())
    first, last = conn.execute(
        "SELECT min(item), max(item) FROM usage WHERE category = 'hour_utc'").fetchone()
    first_day = conn.execute(
        "SELECT min(day) FROM usage WHERE category = 'hour_utc'").fetchone()[0]
    out = {"hours": [], "span": None, "first_day": first_day}
    if not first:
        return out
    lo = max(utc_hour_epoch(first), local_midnight(start))
    hi = min(utc_hour_epoch(last), local_midnight(end + datetime.timedelta(days=1)) - 3600)
    if lo > hi:
        return out
    out["span"] = [lo, hi]
    # Every row is one user in one hour, so COUNT(*) is distinct users.
    out["hours"] = [[utc_hour_epoch(item), n] for item, n in conn.execute("""
        SELECT item, COUNT(*) FROM usage
        WHERE category = 'hour_utc' AND day BETWEEN ? AND ?
        GROUP BY item ORDER BY item""", rng)]
    return out


def summary(conn, cfg, start, end, group, lo, hi):
    rng = (start.isoformat(), end.isoformat())
    t = conn.execute("""
        SELECT
          SUM(CASE WHEN category = 'login'       THEN count ELSE 0 END) AS logins,
          SUM(CASE WHEN category = 'session'     THEN count ELSE 0 END) AS sessions,
          COUNT(DISTINCT CASE WHEN category = 'active' THEN user END)   AS active_users,
          SUM(CASE WHEN category = 'launch'      THEN count ELSE 0 END) AS launches,
          COUNT(DISTINCT CASE WHEN category = 'launch' THEN user END)   AS launch_users,
          SUM(CASE WHEN category = 'launch_fail' THEN count ELSE 0 END) AS failed_launches,
          SUM(CASE WHEN category = 'login' AND user = ? THEN count ELSE 0 END) AS unattributed_logins,
          SUM(CASE WHEN category = 'job'         THEN count ELSE 0 END) AS jobs,
          {wall} AS wall_hours,
          {core} AS core_hours,
          {gpu} AS gpu_hours
        FROM usage WHERE day BETWEEN ? AND ?""".format(wall=WALL_HOURS, core=CORE_HOURS, gpu=GPU_HOURS),
        (UNKNOWN_USER,) + rng).fetchone()
    totals = {k: (t[k] or 0) for k in t.keys()}

    launches = [dict(r) for r in conn.execute("""
        SELECT item AS app,
               SUM(CASE WHEN category = 'launch' THEN count ELSE 0 END) AS launches,
               COUNT(DISTINCT CASE WHEN category = 'launch' THEN user END) AS users,
               SUM(CASE WHEN category = 'launch_fail' THEN count ELSE 0 END) AS failed
        FROM usage
        WHERE category IN ('launch', 'launch_fail') AND day BETWEEN ? AND ?
        GROUP BY item ORDER BY launches DESC, failed DESC, app""", rng)]

    apps = [dict(r) for r in conn.execute("""
        SELECT item AS app, COUNT(*) AS user_days, COUNT(DISTINCT user) AS users,
               SUM(count) AS requests
        FROM usage WHERE category = 'app' AND day BETWEEN ? AND ?
        GROUP BY item ORDER BY user_days DESC, app""", rng)]

    walltime = [dict(r) for r in conn.execute("""
        SELECT item AS app,
               SUM(CASE WHEN category = 'job' THEN count ELSE 0 END) AS jobs,
               COUNT(DISTINCT user) AS users,
               {wall} AS wall_hours,
               {core} AS core_hours,
               {gpu} AS gpu_hours
        FROM usage
        WHERE category IN ('job', 'walltime', 'core_time', 'gpu_time') AND day BETWEEN ? AND ?
        GROUP BY item ORDER BY wall_hours DESC, app""".format(
            wall=WALL_HOURS, core=CORE_HOURS, gpu=GPU_HOURS), rng)]

    top_users = []
    if cfg.get("show_top_users", True):
        top_users = [dict(r) for r in conn.execute("""
            SELECT user,
                   COUNT(DISTINCT CASE WHEN category = 'active' THEN day END) AS days_active,
                   SUM(CASE WHEN category = 'session' THEN count ELSE 0 END) AS sessions,
                   SUM(CASE WHEN category = 'launch'  THEN count ELSE 0 END) AS launches,
                   {wall} AS wall_hours
            FROM usage WHERE day BETWEEN ? AND ? AND user NOT IN (?, '')
            GROUP BY user ORDER BY days_active DESC, sessions DESC, user LIMIT 25""".format(wall=WALL_HOURS),
            rng + (UNKNOWN_USER,))]

    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    last_run = meta.get("last_run")
    return {
        "site_name": cfg.get("site_name"),
        "range": {"start": rng[0], "end": rng[1], "group": group},
        "data": {"first_day": lo, "last_day": hi,
                 "last_collected": int(last_run) if last_run else None,
                 # Collected for at least one cluster, and not turned off since.
                 "server_timezone": server_timezone(),
                 "has_walltime": str(cfg.get("walltime")).lower() != "off" and
                                 any(k.startswith("slurm_watermark:") for k in meta)},
        "totals": totals,
        "series": series(conn, start, end, group),
        "launches": launches,
        "apps": apps,
        "walltime": walltime,
        "heatmap": heatmap(conn, start, end),
        "top_users": top_users,
        "show_top_users": bool(cfg.get("show_top_users", True)),
    }


# Must match LATENCY_EDGES in ood-usage-collect.
LATENCY_EDGES = [0.1, 0.25, 0.5, 1, 2, 5, 10, 30]


def bucket_percentile(counts, p):
    """Index of the histogram bucket holding the p-th percentile."""
    total = sum(counts)
    if not total:
        return None
    need, run = p * total, 0
    for i, n in enumerate(counts):
        run += n
        if run >= need:
            return i
    return len(counts) - 1


def issues(conn, cfg, start, end, group, lo, hi):
    rng = (start.isoformat(), end.isoformat())
    count = "SUM(CASE WHEN category = '{0}' THEN count ELSE 0 END)"
    first_day = conn.execute("SELECT min(day) FROM usage WHERE category = 'req'").fetchone()[0]
    has_timing = conn.execute("SELECT 1 FROM usage WHERE category = 'latency' LIMIT 1").fetchone() is not None

    t = conn.execute("""
        SELECT {req} AS requests, {e5} AS err5xx, {e4} AS err4xx, {slow} AS slow,
               {lat} AS timed, {ps} AS pun_starts, {pf} AS pun_start_fails,
               COUNT(DISTINCT CASE WHEN category = 'http_5xx' AND user != ? THEN user END) AS err5xx_users,
               COUNT(DISTINCT CASE WHEN category = 'slow' AND user != ? THEN user END) AS slow_users,
               COUNT(DISTINCT CASE WHEN category = 'pun_start_fail' AND user != ? THEN user END) AS pun_fail_users
        FROM usage WHERE day BETWEEN ? AND ?""".format(
            req=count.format("req"), e5=count.format("http_5xx"), e4=count.format("http_4xx"),
            slow=count.format("slow"), lat=count.format("latency"), ps=count.format("pun_start"),
            pf=count.format("pun_start_fail")),
        (UNKNOWN_USER,) * 3 + rng).fetchone()
    totals = {k: (t[k] or 0) for k in t.keys()}

    rows = conn.execute("""
        SELECT {p} AS period, {req} AS requests, {e5} AS err5xx, {e4} AS err4xx,
               {slow} AS slow, {pf} AS pun_start_fails
        FROM usage WHERE day BETWEEN ? AND ? GROUP BY period""".format(
            p=PERIOD_SQL[group], req=count.format("req"), e5=count.format("http_5xx"),
            e4=count.format("http_4xx"), slow=count.format("slow"), pf=count.format("pun_start_fail")),
        rng).fetchall()
    by_period = {r["period"]: dict(r) for r in rows}
    empty = {"requests": 0, "err5xx": 0, "err4xx": 0, "slow": 0, "pun_start_fails": 0}
    series_rows = [dict(empty, period=k, **{x: y for x, y in by_period.get(k, {}).items() if x != "period"})
                   for k in all_periods(start, end, group)]

    area_requests = dict(conn.execute("""
        SELECT item, SUM(count) FROM usage WHERE category = 'req' AND day BETWEEN ? AND ?
        GROUP BY item""", rng).fetchall())

    def errors(category, limit):
        out = []
        for item, n, users in conn.execute("""
                SELECT item, SUM(count) AS n, COUNT(DISTINCT CASE WHEN user != ? THEN user END)
                FROM usage WHERE category = ? AND day BETWEEN ? AND ?
                GROUP BY item ORDER BY n DESC, item LIMIT ?""", (UNKNOWN_USER, category) + rng + (limit,)):
            code, _, area = item.partition(" ")
            req = area_requests.get(area) or 0
            out.append({"status": int(code), "area": area, "count": n, "users": users,
                        "share": round(100.0 * n / req, 2) if req else None})
        return out

    hist = {}
    for item, n in conn.execute("""
            SELECT item, SUM(count) FROM usage WHERE category = 'latency' AND day BETWEEN ? AND ?
            GROUP BY item""", rng):
        area, _, b = item.rpartition("|")
        hist.setdefault(area, [0] * (len(LATENCY_EDGES) + 1))[int(b)] += n
    slow_by_area = dict(conn.execute("""
        SELECT item, SUM(count) FROM usage WHERE category = 'slow' AND day BETWEEN ? AND ?
        GROUP BY item""", rng).fetchall())
    latency = sorted(({"area": area, "timed": sum(c), "p50": bucket_percentile(c, 0.5),
                       "p95": bucket_percentile(c, 0.95), "slow": slow_by_area.get(area, 0),
                       "slow_share": round(100.0 * slow_by_area.get(area, 0) / sum(c), 2)}
                      for area, c in hist.items()),
                     key=lambda r: (-r["p95"], -r["slow"], r["area"]))

    users = []
    if cfg.get("show_top_users", True):
        users = [dict(r) for r in conn.execute("""
            SELECT user, {e5} AS err5xx, {e4} AS err4xx, {slow} AS slow, {pf} AS pun_start_fails
            FROM usage
            WHERE category IN ('http_5xx', 'http_4xx', 'slow', 'pun_start_fail')
              AND day BETWEEN ? AND ? AND user NOT IN (?, '')
            GROUP BY user
            ORDER BY err5xx + pun_start_fails DESC, slow DESC, err4xx DESC, user LIMIT 25""".format(
                e5=count.format("http_5xx"), e4=count.format("http_4xx"),
                slow=count.format("slow"), pf=count.format("pun_start_fail")), rng + (UNKNOWN_USER,))]

    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    last_run = meta.get("last_run")
    return {
        "site_name": cfg.get("site_name"),
        "range": {"start": rng[0], "end": rng[1], "group": group},
        "data": {"first_day": lo, "last_day": hi, "issues_first_day": first_day,
                 "has_timing": has_timing, "server_timezone": server_timezone(),
                 "last_collected": int(last_run) if last_run else None},
        "slow_seconds": cfg.get("slow_request_seconds", 3),
        "latency_edges": LATENCY_EDGES,
        "totals": totals,
        "series": series_rows,
        "errors_5xx": errors("http_5xx", 50),
        "errors_4xx": errors("http_4xx", 25),
        "latency": latency,
        "users": users,
        "show_top_users": bool(cfg.get("show_top_users", True)),
    }


def csv_response(start_response, filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([r[h] for h in header])
    body = buf.getvalue().encode("utf-8")
    start_response("200 OK", [
        ("Content-Type", "text/csv; charset=utf-8"),
        ("Content-Disposition", 'attachment; filename="%s"' % filename),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])
    return [body]


def json_response(start_response, status, obj):
    body = json.dumps(obj).encode("utf-8")
    start_response(status, [("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
    return [body]


def application(environ, start_response):
    path = environ.get("PATH_INFO", "") or ""
    if path == "":
        location = environ.get("SCRIPT_NAME", "") + "/"
        start_response("301 Moved Permanently", [("Location", location)])
        return [b""]

    if path in ("/", "/index.html", "/issues"):
        with open(os.path.join(HERE, "templates", "index.html"), "rb") as fh:
            body = fh.read()
        # Shown in the error panel, which can't rely on the API's JSON error
        # surviving the proxy.
        if not os.path.exists(CONFIG_PATH):
            config_status = "(not found, using defaults)"
        elif not os.access(CONFIG_PATH, os.R_OK):
            config_status = "(not readable, using defaults)"
        else:
            config_status = ""
        for token, value in ((b"__CONFIG_PATH__", CONFIG_PATH),
                             (b"__CONFIG_STATUS__", config_status),
                             (b"__DB_PATH__", load_config()["db_path"])):
            body = body.replace(token, html.escape(value).encode("utf-8"))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if not path.startswith("/api/"):
        return json_response(start_response, "404 Not Found", {"error": "Not found"})

    cfg = load_config()
    try:
        conn = connect(cfg["db_path"])
        try:
            start, end, group, lo, hi = read_params(conn, environ)
            if path == "/api/issues":
                return json_response(start_response, "200 OK",
                                     issues(conn, cfg, start, end, group, lo, hi))
            if path == "/api/summary":
                return json_response(start_response, "200 OK",
                                     summary(conn, cfg, start, end, group, lo, hi))
            if path == "/api/series.csv":
                return csv_response(
                    start_response, "ood-usage-%s-%s-by-%s.csv" % (start, end, group),
                    ["period", "active_users", "sessions", "logins", "launches",
                     "wall_hours", "core_hours", "gpu_hours"],
                    series(conn, start, end, group))
            if path == "/api/users.csv":
                if not cfg.get("show_top_users", True):
                    raise HTTPError("403 Forbidden", "Per-user reporting is turned off")
                rows = conn.execute("""
                    SELECT user,
                           COUNT(DISTINCT CASE WHEN category = 'active' THEN day END) AS days_active,
                           SUM(CASE WHEN category = 'session' THEN count ELSE 0 END) AS sessions,
                           SUM(CASE WHEN category = 'login'   THEN count ELSE 0 END) AS logins,
                           SUM(CASE WHEN category = 'launch'  THEN count ELSE 0 END) AS launches,
                           {wall} AS wall_hours,
                           {core} AS core_hours,
                           {gpu} AS gpu_hours
                    FROM usage WHERE day BETWEEN ? AND ? AND user != ''
                    GROUP BY user ORDER BY days_active DESC, user""".format(
                        wall=WALL_HOURS, core=CORE_HOURS, gpu=GPU_HOURS),
                    (start.isoformat(), end.isoformat())).fetchall()
                return csv_response(start_response, "ood-users-%s-%s.csv" % (start, end),
                                    ["user", "days_active", "sessions", "logins", "launches",
                                     "wall_hours", "core_hours", "gpu_hours"], rows)
            raise HTTPError("404 Not Found", "Not found")
        finally:
            conn.close()
    except HTTPError as err:
        return json_response(start_response, err.status, {"error": err.message})
