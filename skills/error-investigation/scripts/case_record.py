# Builds Akamai Error Investigation case records from Reference IDs.
#
# Runs inside the Akamai MCP Server's `execute` sandbox, which provides
# call_tool(name, params). Two runs per Reference ID, because translating
# with forward logs plus the WAF lookups doesn't fit one run's time limit:
#   Run 1: set REFERENCE_ID, leave REQUEST_ID = None -> returns status + request_id
#   Run 2: also set REQUEST_ID to run 1's request_id   -> returns the case record
#
# When `execute` isn't available (tools are exposed directly), this file
# is the spec: make the same tool calls and apply the same field mapping.

# ===================== EDIT ONLY THESE TWO LINES =====================
REFERENCE_ID = "<paste the Reference ID exactly as the user gave it>"
REQUEST_ID = None  # Run 1: None. Run 2: the request_id number from run 1.
# ============== DO NOT CHANGE ANYTHING BELOW THIS LINE ===============

import html, re, time
from urllib.parse import urlsplit

MAX_AGE_S = 48 * 3600  # Edge Diagnostics translates Reference IDs for 48 h only


def normalize(raw):
    s = html.unescape(raw).strip()
    s = re.sub(r"(?i)^reference\s*", "", s).lstrip("#").strip()
    return s.lower()


def age_seconds(rid):
    parts = rid.split(".")
    if len(parts) == 4 and parts[2].isdigit():
        return int(time.time()) - int(parts[2])
    return None  # Global Request Number or unknown shape: let the API decide


def decode_status(line, legend):
    """Expand objectStatus letter codes using the translation's legend."""
    lt = line.get("logType")
    out = {}
    for field in ("objectStatus", "objectStatus2", "objectStatus3"):
        codes = line.get(field)
        key = f"{lt}{field[0].upper()}{field[1:]}"
        table = legend.get(key) or {}
        if codes and codes != "-" and table:
            out[field] = {c: table.get(c, "?") for c in codes}
    return out


def edge_logs(res):
    ll = res.get("logLines") or {}
    legend = ll.get("legend") or {}
    out = []
    for line in ll.get("logs") or []:
        kept = {k: v for k, v in line.items()
                if v not in (None, "-", "") and not (k == "customField" and set(v.replace("%20", "")) <= {"-"})}
        decoded = decode_status(line, legend)
        if decoded:
            kept["object_status_decoded"] = decoded
        out.append(kept)
    return out


def served_by_network(ver, prod, stg):
    """Which Akamai network evaluated the request, inferred from the WAF version that fired.

    The translation has no network field, so this is the only signal. It compares
    against the versions active at check time, not at request time.
    """
    if ver is None or (ver != prod and ver != stg):
        return {"network": "unknown", "basis": "the version that fired is active on neither network now"}
    if ver == prod and ver == stg:
        return {"network": "unknown", "basis": "production and staging run the same version, so either could have served it"}
    net = "production" if ver == prod else "staging"
    other = "staging" if net == "production" else "production"
    return {"network": net,
            "basis": f"version {ver} that fired is the {net} version; {other} runs "
                     f"{stg if net == 'production' else prod}, which did not evaluate this request"}


def waf_block(res):
    policy_id, _, rule_id = res["wafDetails"].partition("||")
    cm = re.search(r"configs/(\d+)/versions/(\d+)", res.get("wafDetailsUrl") or "")
    w = {"security_config_id": int(cm.group(1)) if cm else None,
         "security_config_name": None,
         "security_config_version": int(cm.group(2)) if cm else None,
         "security_config_production_version": None,
         "security_config_staging_version": None,
         "served_by_network": None,
         "policy_id": policy_id or None, "policy_name": None,
         "rule_id": int(rule_id) if rule_id.isdigit() else (rule_id or None),
         "rule_name": None, "rule_conditions": None}
    if not cm:
        return w
    cid, ver = w["security_config_id"], w["security_config_version"]
    # Read-only: every aap_manage_* call here uses operation "get".
    c = call_tool("aap_manage_configurations", {"operation": "get", "config_id": cid})
    cd = c.get("data") or {}
    w["security_config_name"] = cd.get("name")
    w["security_config_production_version"] = cd.get("productionVersion")
    w["security_config_staging_version"] = cd.get("stagingVersion")
    w["served_by_network"] = served_by_network(ver, w["security_config_production_version"],
                                               w["security_config_staging_version"])
    if policy_id:
        p = call_tool("aap_manage_security_policies", {"operation": "get", "config_id": cid,
                      "version_number": ver, "policy_id": policy_id})
        w["policy_name"] = (p.get("data") or {}).get("policyName")
    if isinstance(w["rule_id"], int):
        r = call_tool("aap_manage_custom_rules", {"operation": "get", "config_id": cid,
                      "rule_id": w["rule_id"]})
        if r.get("success"):  # not a custom rule (e.g. managed attack rule) -> name stays null
            d = r["data"]
            w["rule_name"] = d.get("name")
            w["rule_conditions"] = [{"type": x.get("type"), "positive_match": x.get("positiveMatch"),
                                     "values": x.get("value")} for x in d.get("conditions") or []]
    return w


def case_record(rid, res):
    u = urlsplit(res.get("url") or "")
    logs = (res.get("logLines") or {}).get("logs") or []
    fwd = next((l for l in logs if l.get("forwardIp") not in (None, "-")), {})
    m = re.match(r"^(\S+?)\.\s+(.*)$", res.get("reasonForFailure") or "")
    ip = lambda k: (res.get(k) or {}).get("ip")
    rec = {
        "reference": rid,
        "hostname": u.hostname,
        "path": u.path or "/",
        "query": u.query,
        "method": res.get("clientRequestMethod"),
        "cpcode": res.get("cpCode"),
        "property_name": res.get("propertyName"),
        "origin_hostname": fwd.get("forwardHostname") or res.get("cacheKeyHostname"),
        "origin_ip": fwd.get("forwardIp"),
        "http_status": res.get("httpResponseCode"),
        "failure_code": m.group(1) if m else (logs[0].get("error") if logs else None),
        "failure_reason": m.group(2) if m else res.get("reasonForFailure"),
        "request_time": res.get("date"),
        "edge_server_ip": ip("edgeServerIp"),
        "edge_server_location": (res.get("edgeServerIp") or {}).get("ipLocation"),
        "client_ip": ip("clientIp"),
        "client_location": (res.get("clientIp") or {}).get("ipLocation"),
        "user_agent": res.get("userAgent"),
        "waf": waf_block(res) if res.get("wafDetails") else None,
        "edge_logs": edge_logs(res),
    }
    return rec


POLL_BUDGET_S = 20  # keep run 1 well inside the execute time limit


def start_and_poll(raw):
    """Run 1: normalize, age-check, start the translation, poll briefly."""
    rid = normalize(raw)
    age = age_seconds(rid)
    if age is not None and age > MAX_AGE_S:
        return {"reference": rid, "status": "EXPIRED", "age_hours": round(age / 3600, 1)}
    s = call_tool("ediag_translate_err_str", {"error_code": rid, "trace_forward_logs": True})
    if not s.get("success"):
        return {"reference": rid, "status": "REJECTED", "error": s.get("error")}
    d, t0 = s["data"], time.time()
    while d.get("executionStatus") == "IN_PROGRESS" and time.time() - t0 < POLL_BUDGET_S:
        time.sleep(max(d.get("retryAfter", 3), 3))
        d = call_tool("ediag_check_translate_err_status", {"request_id": d["requestId"]})["data"]
    out = {"reference": rid, "status": d.get("executionStatus"), "request_id": d.get("requestId")}
    if out["status"] not in ("SUCCESS", "IN_PROGRESS"):
        out["error"] = d.get("errorResponse")
    return out


def build(rid, request_id):
    """Run 2: fetch the finished translation and build the case record."""
    d = call_tool("ediag_check_translate_err_status", {"request_id": request_id})["data"]
    if d.get("executionStatus") != "SUCCESS":
        return {"reference": rid, "status": d.get("executionStatus"), "request_id": request_id,
                "error": d.get("errorResponse")}
    rec = case_record(rid, d.get("result") or {})
    rec["checked_at_epoch"] = int(time.time())
    return rec


if REFERENCE_ID.startswith("<"):
    result = {"status": "NO_REFERENCE_ID", "error": "REFERENCE_ID was not set. Ask the user for one."}
elif REQUEST_ID is None:
    result = start_and_poll(REFERENCE_ID)
else:
    result = build(normalize(REFERENCE_ID), int(REQUEST_ID))
