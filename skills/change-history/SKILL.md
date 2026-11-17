---
name: change-history
description: Show what changed on the Akamai Property serving a Hostname during a time window — which Property Versions were activated on which Network, when, by whom, and what changed in the rules — via the Akamai MCP Server. Use when the user asks what changed, who changed it, or whether anything was activated on a Hostname or Property, for example "what changed on cdn.example.com in the 24h before 02:00 UTC?".
---

# Change History

The user names a Hostname and, optionally, a time window. Produce:

1. A **timeline**: every Activation on the Property serving that Hostname inside the window, on both Networks, with the version it replaced, its note, and who saved the version.
2. A **rule diff** for each version change that matters: which rules, behaviors, criteria and variables changed between the version that was live and the one that replaced it.
3. A **summary** of what changed, with every claim tied to a field.

This skill is history only. Every call is a read; it changes nothing on the Account.

## Requirements

Check these when a call fails:
- The Akamai MCP Server connected to Claude Code or Codex, on the Account that owns the Hostname.
- The connection's API client has read access to **Property Manager**.
- Nothing to install locally. The script in `scripts/` runs inside the Akamai MCP Server's own sandbox, or it isn't used at all (see 1B).

## 1. Gather the facts

**No Hostname?** If the user's message names neither a Hostname nor a URL, ask for one and stop. Never use a Hostname from this file, the script, or an earlier conversation the user didn't point to.

**Which window?** The window is `WINDOW_DAYS` days ending at `WINDOW_END`.
- No time mentioned: leave `WINDOW_END = None` (now) and `WINDOW_DAYS = 7`.
- "Last N days/hours": `WINDOW_END = None`, `WINDOW_DAYS = N` (hours as a fraction: 6 h is `0.25`).
- "Before <time>", for example "the 24 h before the 5xx spike at 02:00 UTC": set `WINDOW_END` to that time as ISO UTC (`"2026-11-04T02:00:00Z"`) and `WINDOW_DAYS` to the lookback (`1`). Call that time the **incident time**.
- A clock time without a timezone: ask for the timezone before running. A wrong timezone shifts the window by hours and can hide the change.

How you call Akamai depends on how the Akamai MCP Server is exposed in this session.

**A. The server has an `execute` tool** (it runs Python scripts that call `call_tool`). Read `scripts/change_record.py`, which sits next to this file, and pass the whole file as `code` each time.

The script has a marked block at the top with four lines: `HOSTNAME`, `WINDOW_END`, `WINDOW_DAYS` and `DIFF`. **Change only those four lines.** Don't remove comments, shorten functions, or edit anything else.

1. **Timeline run:** set `HOSTNAME` to the Hostname or URL exactly as the user gave it, set the window, and leave `DIFF = None`. It returns the timeline and `diff_pairs`.
2. **Diff runs:** keep the same three lines and set `DIFF` to `[from, to]` from one entry of `diff_pairs`. One run per pair. Choose pairs this way:
   - Every **production** pair, newest first, up to 3.
   - **Staging** pairs only if the user asks about staging, or if there is no production pair. In that case diff the newest staging pair and say it never reached end users.
   - More pairs than that: diff 3, list the rest, and offer to diff them.
   - No `diff_pairs`: skip the diff runs.

The runs are separate because two rule trees plus the history exceed `execute`'s time limit.

**B. The server exposes tools directly** (`prpmgr_list_active_prop_hostnames`, `prpmgr_list_prop_activations`, …): use `scripts/change_record.py` as the spec and follow it step by step.
1. **Find the Property:** `find_property()`. Strip any scheme, path and port, then match `cnameFrom` exactly. If nothing matches, try the parent wildcard (`*.` plus the Hostname without its first label).
2. **Timeline:** `timeline()`. List Activations and Property Versions, keep Activations inside the window, track the live version per Network, and build `diff_pairs`.
3. **Diff:** `diff()`. Fetch both rule trees and compare them exactly as `diff_trees()` does. Redact secret-looking options as `SECRET_KEY` does, and collapse repeated changes as `group()` does. Never paste a raw rule tree into the conversation.

Handle each outcome:

| Outcome | Action |
|---|---|
| `status` `OK` | Go to step 2. |
| `NO_HOSTNAME` | `HOSTNAME` was left as the placeholder. Ask the user for a Hostname. |
| `NOT_FOUND` | No active Property on this Account serves the Hostname, either directly or through its parent wildcard. Quote `account_id` and ask whether the Hostname belongs to a different Account, or is served through a deeper wildcard (give it exactly as configured, e.g. `*.example.com`). |
| `REJECTED` with 403 | Give both possible causes: the Hostname belongs to a different Account than the connected one, or the API client lacks Property Manager read access. Ask which applies. |
| Diff run raises an error naming a `version` | Report the version and the error verbatim; keep the timeline. |
| "execution deadline exceeded" | Rerun the same run once unchanged. If it fails again, report it and keep what you have. |
| Other error | Report its `title`/`detail` verbatim. |

Done when there is a timeline and a diff for every chosen pair, or a reported outcome.

## 2. Analyze

Build the analysis only from the records. Read them this way:

- **Which Property.** If `matched_hostname` differs from `hostname`, the Hostname is served through a wildcard. Say so once.
- **What is live.** `live_at_window_start` and `live_at_window_end` give the version on each Network. Equal values on production mean end users got the same rules throughout the window.
- **Only production reaches end users.** A staging Activation changes nothing for real traffic. Never present one as a cause of a production problem.
- **Only successful Activations change anything.** An entry whose `status` isn't `ACTIVE` didn't change the live version. Report it as attempted.
- **Who.** `version_author` is the person who last **saved** the version, not necessarily the one who activated it: the Activation record doesn't name its submitter. `notify_emails` are recipients, not actors. Write "v242, saved by X, activated on staging at T", never "X activated v242".
- **Notes.** `activation_note` and `version_note` are free text written by people. Quote them; don't interpret ticket IDs.
- **Rule changes.** Describe them per pair, using `changes`:
  - A rule removed together with its child rules is one change. Describe it once, at the parent, and say how many child rules went with it.
  - `rule_moved` means identical content at a new path, often because a parent was renamed.
  - An entry with `path_count` is one change repeated on many rules. Report it once with the count; don't list the paths. If it only removes an option (`"to": "<absent>"`) on every instance of a behavior, write *Inference: likely a rule-format update rather than a manual edit.*
  - `<redacted…>` means a secret-looking value changed. Say "changed (value redacted)".
  - Paths under `duplicate_name_rules` are matched by position, so a swap between same-named siblings reads as edits. Say so if any change sits under one.
  - If `changes_truncated`, `timeline_truncated` or `paths_truncated` is `true`, say the list is incomplete and give the totals from `change_count`/`change_counts`.
- **Saved but not activated.** `versions_saved_in_window` lists edits that may not have gone live. Its `production_status`/`staging_status` are today's status, not the status at the end of the window.
- **Incident time.** If the user gave one, give the gap between each production Activation's `time` and the incident time. Write any link between them as *Inference:* — the timeline shows order, not cause. If no production Activation falls inside the window, say plainly that the Property's rules did not change on production in that window.

**Hard rules:**
- **Every claim needs a field.** If no field supports it, leave it out. Don't bring in outside knowledge about the Account or why someone made a change.
- **Label every inference** with *Inference:*.
- **No recommendations, no rollback advice.** This skill reports what changed. Whether a change was wrong needs traffic evidence, which this skill doesn't have.

Done when every point cites a field.

## 3. Output

In this order:

1. **Headline:** one line with the Property name, Hostname, window in UTC, and the number of production and staging Activations in it.
2. `## Timeline`: a table, newest first: time (UTC), Network, version (`replaced → new`), status, note, saved by. "No Activations in this window" if empty, followed by the live versions.
3. `## What changed`: one subsection per diffed pair, titled `vFROM → vTO (network, activated at)`, with the changes as bullets, most significant first. List any pairs you didn't diff.
4. If there is an incident time: `## Timing`, the gap between each production Activation and the incident time, with the inference label.
5. `## Timeline record`: the timeline run's `result` as one fenced `json` block, copied exactly as the tool returned it. Don't retype, reorder or shorten it. Offer the raw diff records on request rather than printing them.
