# Builds an Akamai Change History record for the Property serving a Hostname.
#
# Runs inside the Akamai MCP Server's `execute` sandbox, which provides
# call_tool(name, params). Two kinds of run, because fetching two large rule
# trees on top of the history doesn't fit one run's time limit:
#   Timeline run: set HOSTNAME (and the window), leave DIFF = None -> Activations, versions, diff_pairs
#   Diff run:     also set DIFF to one pair from diff_pairs        -> rule-level diff of the two versions
#
# When `execute` isn't available (tools are exposed directly), this file
# is the spec: make the same tool calls and apply the same logic.

# ==================== EDIT ONLY THESE FOUR LINES ====================
HOSTNAME = "<paste the Hostname exactly as the user gave it>"
WINDOW_END = None  # None = now. Otherwise an ISO time ("2026-11-04T02:00:00Z") or Unix epoch seconds.
WINDOW_DAYS = 7    # How far back from WINDOW_END to look.
DIFF = None        # Timeline run: None. Diff run: [from_version, to_version], e.g. [272, 274].
# ============== DO NOT CHANGE ANYTHING BELOW THIS LINE ===============

import json, re, time
from datetime import datetime, timezone

MAX_TIMELINE = 60      # newest entries kept in the timeline
MAX_BYTES = 60000      # diff output budget; changes beyond it are counted, not listed
MAX_STR = 400          # longer strings are cut, with an explicit marker
MAX_PATHS = 25         # paths listed for one change repeated on many rules; the rest are counted
SECRET_KEY = re.compile(r"(?i)(secret|passw|token|salt|private|credential|hmac|apikey|accesskey|"
                        r"signingkey|encryptionkey|transitionkey|^key$)")


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


def ts(s):
    return to_epoch(s) if s else None


# ---------- Property lookup ----------

def active_hostnames(name):
    r = call_tool("prpmgr_list_active_prop_hostnames", {"hostname": name})
    if not r.get("success"):
        return None, None, r.get("error") or r.get("message")
    d = r.get("data") or {}
    items = [i for i in (d.get("hostnames") or {}).get("items") or []
             if (i.get("cnameFrom") or "").lower() == name]
    return items, d.get("accountId"), None


def find_property(host):
    h = host.strip().lower().rstrip(".")
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split(":", 1)[0]
    items, account, error = active_hostnames(h)
    if error:
        return None, {"status": "REJECTED", "hostname": h, "error": error}
    matched = h
    # A Hostname served only through a wildcard (cod.example.com via *.example.com) has no exact entry.
    if not items and not h.startswith("*.") and h.count(".") >= 2:
        matched = "*." + h.split(".", 1)[1]
        items, account2, error = active_hostnames(matched)
        account = account or account2
        if error:
            return None, {"status": "REJECTED", "hostname": h, "error": error}
    if not items:
        return None, {"status": "NOT_FOUND", "hostname": h, "account_id": account}
    by_net = {}
    for i in items:
        net = "production" if i.get("productionCnameTo") or i.get("productionEdgeHostnameId") else "staging"
        by_net[net] = i
    main = by_net.get("production") or by_net["staging"]
    prop = {"hostname": h, "matched_hostname": matched, "account_id": account,
            "property_id": main["propertyId"], "property_name": main.get("propertyName"),
            "contract_id": main["contractId"], "group_id": main["groupId"],
            "latest_version": main.get("latestVersion"),
            "active_on": sorted(by_net)}
    stg = by_net.get("staging")
    if stg and stg["propertyId"] != main["propertyId"]:
        prop["staging_property"] = {"property_id": stg["propertyId"], "property_name": stg.get("propertyName")}
    return prop, None


def ids(prop):
    return {"property_id": prop["property_id"], "contract_id": prop["contract_id"], "group_id": prop["group_id"]}


# ---------- timeline ----------

def live_at(acts, network, epoch):
    """Version live on a Network at a time: the last successful Activation (or deactivation) before it."""
    live = None
    for a in acts:
        if a["network"] != network or a.get("status") != "ACTIVE" or (a["_t"] or 0) > epoch:
            continue
        live = a["propertyVersion"] if a.get("activationType") == "ACTIVATE" else None
    return live


def timeline(prop, end, days):
    start = end - int(days * 86400)
    P = ids(prop)
    acts = call_tool("prpmgr_list_prop_activations", P)["data"]["activations"]["items"]
    vers = call_tool("prpmgr_list_prop_versions", P)["data"]["versions"]["items"]
    vmap = {v["propertyVersion"]: v for v in vers}
    for a in acts:
        a["_t"] = ts(a.get("updateDate") or a.get("submitDate"))
    acts.sort(key=lambda a: a["_t"] or 0)

    def author(v):
        x = vmap.get(v) or {}
        return {"version_author": x.get("updatedByUser"), "version_saved": x.get("updatedDate"),
                "version_note": x.get("note") or None}

    events, pairs = [], []
    prev = {"PRODUCTION": live_at(acts, "PRODUCTION", start), "STAGING": live_at(acts, "STAGING", start)}
    for a in acts:
        net = a["network"]
        if not a["_t"] or a["_t"] <= start:
            continue
        if a["_t"] > end:
            break
        e = {"time": a.get("updateDate"), "submitted": a.get("submitDate"), "network": net.lower(),
             "type": a.get("activationType"), "status": a.get("status"), "version": a["propertyVersion"],
             "replaced_version": prev[net], "activation_note": a.get("note") or None,
             "notify_emails": a.get("notifyEmails") or []}
        e.update(author(a["propertyVersion"]))
        events.append(e)
        if a.get("status") != "ACTIVE":
            continue  # failed or pending: the live version didn't change
        if a.get("activationType") == "ACTIVATE":
            if prev[net] is not None and prev[net] != a["propertyVersion"]:
                pairs.append({"network": net.lower(), "from": prev[net], "to": a["propertyVersion"],
                              "activated_at": a.get("updateDate")})
            prev[net] = a["propertyVersion"]
        else:
            prev[net] = None

    saved = [{"version": v["propertyVersion"], "author": v.get("updatedByUser"), "saved": v.get("updatedDate"),
              "note": v.get("note") or None, "production_status": v.get("productionStatus"),
              "staging_status": v.get("stagingStatus")}
             for v in vers if start < (ts(v.get("updatedDate")) or 0) <= end]
    saved.sort(key=lambda x: x["version"])

    events.reverse()  # newest first
    return {
        "status": "OK",
        "property": prop,
        "window": {"start": iso(start), "end": iso(end), "days": days},
        "live_at_window_start": {"production": live_at(acts, "PRODUCTION", start),
                                 "staging": live_at(acts, "STAGING", start)},
        "live_at_window_end": {"production": live_at(acts, "PRODUCTION", end),
                               "staging": live_at(acts, "STAGING", end)},
        "activations_in_window": len(events),
        "timeline_truncated": len(events) > MAX_TIMELINE,
        "timeline": events[:MAX_TIMELINE],
        "versions_saved_in_window": saved,
        "diff_pairs": pairs[::-1],  # newest first
        "history_totals": {"versions": len(vers), "activations": len(acts)},
    }


# ---------- diff ----------

def redact(key, val):
    return "<redacted>" if isinstance(key, str) and SECRET_KEY.search(key) else val


def clip(v):
    if isinstance(v, str) and len(v) > MAX_STR:
        return v[:MAX_STR] + f"…<cut, {len(v)} chars total>"
    return v


def clean(obj, key=None):
    """Redact secret-looking options, clip long strings, keep structure."""
    if isinstance(obj, dict):
        return {k: clean(redact(k, v), k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean(x, key) for x in obj]
    return clip(obj)


def flatten(rule, path, out, order):
    out[path] = rule
    names, seen = [], {}
    for c in rule.get("children") or []:
        n = c.get("name", "?")
        seen[n] = seen.get(n, 0) + 1
        label = n if seen[n] == 1 else f"{n}[{seen[n]}]"
        names.append(label)
        flatten(c, f"{path} > {label}", out, order)
    order[path] = names
    return out


def occurrences(items):
    """Key a list of behaviors/criteria by name plus occurrence, e.g. 'caching', 'caching[2]'."""
    seen, out = {}, {}
    for it in items or []:
        n = it.get("name", "?")
        seen[n] = seen.get(n, 0) + 1
        out[n if seen[n] == 1 else f"{n}[{seen[n]}]"] = it
    return out


def option_changes(a, b):
    out = {}
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k, "<absent>"), b.get(k, "<absent>")
        if x == y:
            continue
        if SECRET_KEY.search(k):
            out[k] = "<redacted: value changed>"
        elif isinstance(x, list) and isinstance(y, list):
            out[k] = {"added": clean([i for i in y if i not in x], k), "removed": clean([i for i in x if i not in y], k)}
        else:
            out[k] = {"from": clean(x, k), "to": clean(y, k)}
    return out


def list_changes(kind, old, new):
    o, n = occurrences(old), occurrences(new)
    ch = []
    for k in n:
        if k not in o:
            ch.append({"change": f"{kind}_added", kind: k, "options": clean(n[k].get("options") or {})})
        elif o[k] != n[k]:
            oc = option_changes(o[k].get("options") or {}, n[k].get("options") or {})
            ch.append({"change": f"{kind}_changed", kind: k, "options": oc or "<non-option fields changed>"})
    for k in o:
        if k not in n:
            ch.append({"change": f"{kind}_removed", kind: k, "options": clean(o[k].get("options") or {})})
    common_o = [k for k in o if k in n]
    common_n = [k for k in n if k in o]
    if common_o != common_n:
        ch.append({"change": f"{kind}_order_changed", "from": common_o, "to": common_n})
    return ch


def rule_summary(r):
    return {"criteria_must_satisfy": r.get("criteriaMustSatisfy"),
            "criteria": [{"name": c.get("name"), "options": clean(c.get("options") or {})} for c in r.get("criteria") or []],
            "behaviors": [{"name": b.get("name"), "options": clean(b.get("options") or {})} for b in r.get("behaviors") or []],
            "children": [c.get("name") for c in r.get("children") or []],
            "comments": clip(r.get("comments") or None)}


def strip_children(r):
    return {k: v for k, v in r.items() if k != "children"}


def diff_trees(old, new):
    fo, oo = {}, {}
    fn, on = {}, {}
    flatten(old, "default", fo, oo)
    flatten(new, "default", fn, on)
    changes = []
    removed = [p for p in fo if p not in fn]
    added = [p for p in fn if p not in fo]
    # A rule that disappears in one place and appears with identical content elsewhere was moved.
    moved = []
    for p in list(removed):
        for q in list(added):
            if strip_children(fo[p]) == strip_children(fn[q]):
                moved.append({"change": "rule_moved", "from_path": p, "to_path": q})
                removed.remove(p)
                added.remove(q)
                break
    changes += moved
    changes += [{"change": "rule_added", "path": p, "rule": rule_summary(fn[p])} for p in added]
    changes += [{"change": "rule_removed", "path": p, "rule": rule_summary(fo[p])} for p in removed]
    for p in fn:
        if p not in fo:
            continue
        a, b = fo[p], fn[p]
        for c in list_changes("behavior", a.get("behaviors"), b.get("behaviors")):
            changes.append(dict(c, path=p))
        for c in list_changes("criterion", a.get("criteria"), b.get("criteria")):
            changes.append(dict(c, path=p))
        if a.get("criteriaMustSatisfy") != b.get("criteriaMustSatisfy"):
            changes.append({"change": "criteria_must_satisfy_changed", "path": p,
                            "from": a.get("criteriaMustSatisfy"), "to": b.get("criteriaMustSatisfy")})
        if a.get("comments") != b.get("comments"):
            changes.append({"change": "comments_changed", "path": p,
                            "from": clip(a.get("comments")), "to": clip(b.get("comments"))})
        if (a.get("options") or {}) != (b.get("options") or {}):
            changes.append({"change": "rule_options_changed", "path": p,
                            "options": option_changes(a.get("options") or {}, b.get("options") or {})})
        ko = [x for x in oo.get(p, []) if x in on.get(p, [])]
        kn = [x for x in on.get(p, []) if x in oo.get(p, [])]
        if ko != kn:
            changes.append({"change": "child_rules_reordered", "path": p, "from": ko, "to": kn})
    # Property variables live on the default rule.
    vo = {v.get("name"): v for v in old.get("variables") or []}
    vn = {v.get("name"): v for v in new.get("variables") or []}
    for k in sorted(set(vo) | set(vn)):
        x, y = vo.get(k), vn.get(k)
        if x == y:
            continue
        sens = (x or {}).get("sensitive") or (y or {}).get("sensitive")
        show = (lambda v: None if v is None else ({**v, "value": "<redacted>"} if sens else clean(v)))
        changes.append({"change": "variable_" + ("added" if x is None else "removed" if y is None else "changed"),
                        "variable": k, "from": show(x), "to": show(y)})
    # Sibling rules with the same name are matched by position, so a swap between them reads as edits.
    dups = sorted({p.rsplit("[", 1)[0] for p in list(fo) + list(fn) if p.endswith("]") and "[" in p.rsplit(" > ", 1)[-1]})
    return changes, len(fo), len(fn), dups


def rules(prop, v):
    r = call_tool("prpmgr_get_prop_rules", dict(ids(prop), property_version=int(v)))
    if not r.get("success"):
        raise RuntimeError(json.dumps({"version": v, "error": r.get("error") or r.get("message")})[:500])
    return r["data"]


def group(changes):
    """Collapse identical changes made at several paths into one entry.

    A rule-format cleanup or a bulk edit repeats the same change on dozens of
    rules; listing each one buries the edits that matter. Order of first
    appearance is kept.
    """
    groups = {}
    for c in changes:
        p = c.get("path")
        key = json.dumps({k: v for k, v in c.items() if k != "path"}, sort_keys=True, default=str)
        if p is None or key not in groups:
            groups[key if p is not None else id(c)] = dict(c, _paths=[p] if p is not None else None)
        else:
            groups[key]["_paths"].append(p)
    out = []
    for g in groups.values():
        paths = g.pop("_paths")
        if paths and len(paths) > 1:
            g.pop("path", None)
            g["path_count"] = len(paths)
            g["paths"] = paths[:MAX_PATHS]
            if len(paths) > MAX_PATHS:
                g["paths_truncated"] = True
        out.append(g)
    return out


def diff(prop, pair):
    f, t = int(pair[0]), int(pair[1])
    ro, rn = rules(prop, f), rules(prop, t)
    changes, n_old, n_new, dups = diff_trees(ro["rules"], rn["rules"])
    counts = {}
    for c in changes:
        counts[c["change"]] = counts.get(c["change"], 0) + 1
    grouped = group(changes)
    kept, size = [], 0
    for c in grouped:
        size += len(json.dumps(c))
        if size <= MAX_BYTES:
            kept.append(c)
    return {"status": "OK", "property": prop, "from_version": f, "to_version": t,
            "rule_format": {"from": ro.get("ruleFormat"), "to": rn.get("ruleFormat")},
            "rule_nodes": {"from": n_old, "to": n_new},
            "change_count": len(changes), "change_counts": counts,
            "grouped_entries": len(grouped),
            "duplicate_name_rules": dups,
            "changes_truncated": len(kept) < len(grouped),
            "changes": kept,
            "checked_at": iso(int(time.time()))}


# ---------- main ----------

if HOSTNAME.startswith("<"):
    result = {"status": "NO_HOSTNAME", "error": "HOSTNAME was not set. Ask the user for one."}
else:
    prop, err = find_property(HOSTNAME)
    if err:
        result = err
    elif DIFF is None:
        result = timeline(prop, to_epoch(WINDOW_END), float(WINDOW_DAYS))
    else:
        result = diff(prop, DIFF)
