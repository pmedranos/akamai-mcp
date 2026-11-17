# Finds when the error rate of a Hostname rose above its own baseline, from
# Akamai's delivery traffic report (Reporting API v2, delivery/traffic/current).
#
# Runs inside the Akamai MCP Server's `execute` sandbox, which provides
# call_tool(name, params). One run: a time series for the Hostname, spike
# detection in the sandbox, then a response-code breakdown for each episode.
# Only a summary comes back, never the raw rows.
#
# When `execute` isn't available (tools are exposed directly), this file
# is the spec: make the same tool calls and apply the same logic.

# ==================== EDIT ONLY THESE FOUR LINES ====================
HOSTNAME = "<paste the Hostname exactly as the user gave it>"
WINDOW_END = None    # None = now. Otherwise an ISO time ("2026-11-04T02:00:00Z") or Unix epoch seconds.
WINDOW_DAYS = 1      # How far back from WINDOW_END to look (max 92). Hours as a fraction: 6 h = 0.25.
ERROR_CLASS = "5xx"  # Response class to track: "5xx" or "4xx".
# ============== DO NOT CHANGE ANYTHING BELOW THIS LINE ===============

import json, time
from datetime import datetime, timezone

REPORT = {"productFamily": "delivery", "reportingArea": "traffic", "report": "current"}
RETENTION_DAYS = 92
REFERENCE_DAYS = 7     # baseline comes from this many days before the window (hourly), so a long incident
                       # covering the whole window still shows as one
MIN_REFERENCE_HOURS = 24  # fewer hours of reference traffic: fall back to the window's own baseline
MIN_ERRORS = 30        # a bucket with fewer errors than this is never a spike (low-traffic noise)
SPIKE_FACTOR = 2.0     # spike: rate >= baseline * SPIKE_FACTOR ...
SPIKE_MIN_DELTA = 0.01 # ... and rate >= baseline + 1 percentage point
MAX_EPISODES = 2       # episodes that get a response-code breakdown (call budget)
BREAKDOWN_DEADLINE = 6 # seconds into the run after which no more breakdowns start (execute's time budget)
MAX_POINTS = 48        # points in the downsampled series returned for description/charting
MAX_CODES = 8          # response codes listed per episode


# ---------- time ----------

def to_epoch(v):
    if v is None:
        return int(time.time())
    if isinstance(v, (int, float)) or str(v).strip().isdigit():
        return int(v)
    s = str(v).strip().replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def granularity(days):
    """Smallest bucket that keeps the series a manageable size."""
    if days <= 2:
        return "time5minutes", 300
    if days <= 14:
        return "time1hour", 3600
    return "time1day", 86400


# ---------- report calls ----------

def query(start, end, dimensions, metrics, filters, limit):
    r = call_tool("report_get_report_data", dict(REPORT, start=iso(start), end=iso(end), dimensions=dimensions,
                                                 metrics=metrics, filters=filters, limit=limit))
    if not r.get("success"):
        raise RuntimeError(json.dumps({"error": r.get("error") or r.get("message")})[:500])
    return r.get("data") or [], r.get("warning")


def host_filter(h):
    return {"dimensionName": "hostname", "operator": "IN_LIST", "expressions": [h]}


# ---------- analysis ----------

def quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def pct(x):
    return round(100.0 * x, 3)


def series(rows, tdim):
    b = {}
    for r in rows:
        x = b.setdefault(int(r[tdim]), {"hits": 0, "errors": 0, "errors_to_origin": 0})
        x["hits"] += r.get("edgeHitsSum") or 0
        if r.get("responseClass") == ERROR_CLASS:
            x["errors"] += r.get("edgeHitsSum") or 0
            x["errors_to_origin"] += r.get("originHitsSum") or 0
    return sorted(b.items())


def episodes(pts, step, baseline):
    threshold = max(baseline * SPIKE_FACTOR, baseline + SPIKE_MIN_DELTA)
    out, cur = [], None
    for t, x in pts:
        rate = x["errors"] / x["hits"] if x["hits"] else 0.0
        hot = x["errors"] >= MIN_ERRORS and rate >= threshold
        if hot:
            if cur and t == cur["_last"] + step:
                cur["_last"] = t
            else:
                cur = {"_first": t, "_last": t, "buckets": 0, "hits": 0, "errors": 0, "errors_to_origin": 0,
                       "peak_rate": 0.0, "_peak_t": t}
                out.append(cur)
            cur["buckets"] += 1
            for k in ("hits", "errors", "errors_to_origin"):
                cur[k] += x[k]
            if rate > cur["peak_rate"]:
                cur["peak_rate"], cur["_peak_t"] = rate, t
        else:
            cur = None
    return out, threshold


def rise_began(pts, ep, baseline):
    """Walk back from the episode start through buckets already rising (rate >= baseline * SPIKE_FACTOR,
    enough errors) but still under the threshold: the error rate often ramps up before it crosses it."""
    idx = {t: i for i, (t, _) in enumerate(pts)}
    i = idx[ep["_first"]]
    while i > 0:
        x = pts[i - 1][1]
        rate = x["errors"] / x["hits"] if x["hits"] else 0.0
        if x["errors"] < MIN_ERRORS or rate < max(baseline * SPIKE_FACTOR, 1e-9):
            break
        i -= 1
    return pts[i][0]


def code_breakdown(h, ep, step):
    rows, warn = query(ep["_first"], ep["_last"] + step, ["responseCode"], ["edgeHitsSum", "originHitsSum"],
                       [host_filter(h), {"dimensionName": "responseClass", "operator": "IN_LIST",
                                         "expressions": [ERROR_CLASS]}], 100)
    rows.sort(key=lambda r: -(r.get("edgeHitsSum") or 0))
    return [{"code": r.get("responseCode"), "hits": r.get("edgeHitsSum"), "to_origin": r.get("originHitsSum")}
            for r in rows[:MAX_CODES]], len(rows) > MAX_CODES or bool(warn)


def reference(h, start, now):
    """Hourly error rate over the REFERENCE_DAYS before the window: the Hostname's normal level."""
    end = start // 3600 * 3600
    oldest = (now - RETENTION_DAYS * 86400) // 3600 * 3600 + 3600
    begin = max(end - REFERENCE_DAYS * 86400, oldest)
    out = {"start": iso(begin), "end": iso(end), "hours": 0, "rate_pct": None, "baseline": None, "usable": False}
    if end - begin < 3600:
        return out
    rows, _ = query(begin, end, ["time1hour", "responseClass"], ["edgeHitsSum", "originHitsSum"], [host_filter(h)], 50000)
    pts = [x for _, x in series(rows, "time1hour") if x["hits"]]
    hits, errs = sum(x["hits"] for x in pts), sum(x["errors"] for x in pts)
    out.update(hours=len(pts), rate_pct=pct(errs / hits) if hits else None,
               baseline=quantile([x["errors"] / x["hits"] for x in pts], 0.25),
               usable=len(pts) >= MIN_REFERENCE_HOURS)
    return out


def downsample(pts, step):
    n = max(1, -(-len(pts) // MAX_POINTS))
    out = []
    for i in range(0, len(pts), n):
        chunk = pts[i:i + n]
        hits = sum(x["hits"] for _, x in chunk)
        errs = sum(x["errors"] for _, x in chunk)
        out.append([iso(chunk[0][0]), hits, errs, pct(errs / hits) if hits else 0.0])
    return out, n * step


def spike_record(host, end, days):
    h = host.strip().lower().rstrip(".")
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split(":", 1)[0]
    if h.startswith("*."):
        return {"status": "WILDCARD", "hostname": h,
                "error": "Traffic is reported per requested Hostname. Ask for a concrete Hostname."}
    started = time.time()
    now = int(started)
    days = min(float(days), RETENTION_DAYS)
    tdim, step = granularity(days)
    end = min(end, now) // step * step
    oldest = (now - RETENTION_DAYS * 86400) // step * step + step  # first full bucket still retained
    start = max((end - int(days * 86400)) // step * step, oldest)
    if end - start < step:
        return {"status": "OUT_OF_RETENTION", "hostname": h, "window_end": iso(end),
                "error": f"The traffic report keeps {RETENTION_DAYS} days of data."}
    ref = reference(h, start, now)
    rows, warn = query(start, end, [tdim, "responseClass"], ["edgeHitsSum", "originHitsSum"], [host_filter(h)], 50000)
    pts = series(rows, tdim)
    total = sum(x["hits"] for _, x in pts)
    if not total:
        return {"status": "NO_TRAFFIC", "hostname": h, "window": {"start": iso(start), "end": iso(end)}}
    errs = sum(x["errors"] for _, x in pts)
    rates = [x["errors"] / x["hits"] for _, x in pts if x["hits"]]
    window_p25 = quantile(rates, 0.25)
    baseline = ref["baseline"] if ref["usable"] else window_p25
    eps, threshold = episodes(pts, step, baseline)
    hot = sum(e["buckets"] for e in eps)
    eps.sort(key=lambda e: -(e["errors"] - baseline * e["hits"]))  # most excess errors first
    out_eps = []
    for i, e in enumerate(eps):
        o = {"start": iso(e["_first"]), "end": iso(e["_last"] + step),
             "rise_began": iso(rise_began(pts, e, baseline)),
             "ongoing_at_window_end": e["_last"] + step >= end,
             "starts_at_window_start": e["_first"] <= start,
             "buckets": e["buckets"], "hits": e["hits"], "errors": e["errors"],
             "error_rate_pct": pct(e["errors"] / e["hits"]) if e["hits"] else 0.0,
             "peak_rate_pct": pct(e["peak_rate"]), "peak_at": iso(e["_peak_t"]),
             "excess_errors": int(e["errors"] - baseline * e["hits"]),
             "errors_reaching_origin_pct": pct(e["errors_to_origin"] / e["errors"]) if e["errors"] else None}
        if i >= MAX_EPISODES:
            o["response_codes_skipped"] = "episode limit"
        elif time.time() - started > BREAKDOWN_DEADLINE:
            o["response_codes_skipped"] = "time budget"
        else:
            try:
                o["response_codes"], o["response_codes_truncated"] = code_breakdown(h, e, step)
            except Exception as ex:
                o["response_codes_skipped"] = "error: " + str(ex)[:200]
        out_eps.append(o)
    out_eps.sort(key=lambda o: o["start"])
    points, point_secs = downsample(pts, step)
    return {
        "status": "OK",
        "hostname": h,
        "error_class": ERROR_CLASS,
        "window": {"start": iso(start), "end": iso(end), "days": round((end - start) / 86400, 3),
                   "granularity": tdim, "buckets": len(pts)},
        "totals": {"hits": total, "errors": errs, "error_rate_pct": pct(errs / total)},
        "baseline_rate_pct": pct(baseline),
        "baseline_source": "reference" if ref["usable"] else "window",
        "reference": {"start": ref["start"], "end": ref["end"], "hours_with_traffic": ref["hours"],
                      "error_rate_pct": ref["rate_pct"]},
        "window_p25_rate_pct": pct(window_p25),
        "elevated_throughout": window_p25 >= threshold,
        "spike_threshold_pct": pct(threshold),
        "episode_count": len(eps),
        "baseline_unreliable": not ref["usable"] and hot > 0.5 * len(pts),
        "episodes": out_eps,
        "series": {"point_seconds": point_secs, "columns": ["start", "hits", "errors", "error_rate_pct"],
                   "points": points},
        "report_truncated": bool(warn),
        "checked_at": iso(now),
    }


# ---------- main ----------

if HOSTNAME.startswith("<"):
    result = {"status": "NO_HOSTNAME", "error": "HOSTNAME was not set. Ask the user for one."}
elif ERROR_CLASS not in ("5xx", "4xx"):
    result = {"status": "BAD_INPUT", "error": "ERROR_CLASS must be \"5xx\" or \"4xx\"."}
else:
    result = spike_record(HOSTNAME, to_epoch(WINDOW_END), WINDOW_DAYS)
