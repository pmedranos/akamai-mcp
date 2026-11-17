---
name: error-spike
description: Find when a Hostname's 5xx (or 4xx) error rate rose above its normal level, how far, which response codes, and whether the failing requests reached origin, from Akamai's traffic report via the Akamai MCP Server. Use when the user asks whether, when or how badly errors spiked on a Hostname, or needs the incident time before checking what changed (the change-history skill).
---

# Error Spike

The user names a Hostname and, optionally, a time window. Produce:

1. A **spike record**: the Hostname's normal error rate, every episode where the rate rose clearly above it (start, end, peak, response codes, share reaching origin), and a compact series of the window.
2. An **analysis** tied to the record, ending with the **incident time** the change-history skill needs.

This skill is reporting only. Every call is a read; it changes nothing on the Account.

## Requirements

Check these when a call fails:
- The Akamai MCP Server connected to Claude Code or Codex, on the Account that serves the Hostname.
- The connection's API client has access to **Reporting** (the `delivery/traffic/current` report).
- Nothing to install locally. The script in `scripts/` runs inside the Akamai MCP Server's own sandbox, or it isn't used at all (see 1B).

## What the data can and can't say

- Traffic is reported per **requested Hostname**, in 5-minute, hourly or daily buckets, for the last **92 days**, about 10 minutes behind real time.
- There is **no URL breakdown**. The Akamai MCP Server exposes no URL reports, so this skill can't say which URLs failed. For one failing request, use the error-investigation skill with its Reference ID.
- Response codes are the ones the **edge** returned to clients.

## 1. Gather the facts

**No Hostname?** If the user's message names neither a Hostname nor a URL, ask for one and stop. A wildcard (`*.example.com`) isn't enough: traffic is reported per concrete Hostname, so ask which one. Never use a Hostname from this file, the script, or an earlier conversation the user didn't point to.

**Which window?** The window is `WINDOW_DAYS` days ending at `WINDOW_END`. The bucket size follows from its length: up to 2 days → 5 minutes, up to 14 → hourly, longer → daily.
- No time mentioned: `WINDOW_END = None` (now) and `WINDOW_DAYS = 1`.
- "Last N hours/days": `WINDOW_END = None`, `WINDOW_DAYS = N` (hours as a fraction: 6 h is `0.25`).
- "Around <time>" or "on <date>": set `WINDOW_END` a few hours after it as ISO UTC (`"2026-11-04T06:00:00Z"`) and `WINDOW_DAYS` to cover it.
- A clock time without a timezone: ask for the timezone before running.

**Which errors?** `ERROR_CLASS = "5xx"` unless the user asks about 4xx.

How you call Akamai depends on how the Akamai MCP Server is exposed in this session.

**A. The server has an `execute` tool** (it runs Python scripts that call `call_tool`). Read `scripts/spike_record.py`, which sits next to this file, and pass the whole file as `code`.

The script has a marked block at the top with four lines: `HOSTNAME`, `WINDOW_END`, `WINDOW_DAYS` and `ERROR_CLASS`. **Change only those four lines.** Don't remove comments, shorten functions, or edit anything else.

1. **Main run** with the window above.
2. **Follow-up runs**, at most two in total, only when the record calls for them:
   - **Widen:** an episode has `starts_at_window_start: true`. The incident began before the window, so its start is unknown. Rerun with `WINDOW_DAYS` doubled (max 92), same `WINDOW_END`.
   - **Zoom:** the window used `time1hour` or `time1day`, and the user needs the start time, for example to check what changed. Rerun for the episode that matters (the one the user asked about, else the largest `excess_errors`) with `WINDOW_END` = its `rise_began` + 3 hours and `WINDOW_DAYS = 0.25`. That gives 5-minute buckets around the start.

**B. The server exposes tools directly** (`report_get_report_data`, …): use `scripts/spike_record.py` as the spec and follow it step by step.
1. **Reference:** query the 7 days before the window, hourly, by `responseClass`, filtered to the Hostname. Its 25th-percentile error rate is the baseline (`reference()`).
2. **Window:** query the window at the bucket size above, by `responseClass`. Detect episodes exactly as `episodes()` and `rise_began()` do.
3. **Codes:** for up to 2 episodes, query `responseCode` filtered to the Hostname and `ERROR_CLASS` (`code_breakdown()`).
4. **Build the record** with exactly the fields of `spike_record()`. Never paste raw report rows into the conversation.

Handle each outcome:

| Outcome | Action |
|---|---|
| `status` `OK` | Go to step 2. |
| `NO_HOSTNAME` | `HOSTNAME` was left as the placeholder. Ask the user for a Hostname. |
| `WILDCARD` | Ask which concrete Hostname under the wildcard. |
| `NO_TRAFFIC` | The traffic report has no requests for this Hostname in the window. Check the spelling, and whether the Hostname is served by the connected Account. |
| `OUT_OF_RETENTION` | The window ends more than 92 days ago. Say so; there's no data to query. |
| `BAD_INPUT` | `ERROR_CLASS` must be `"5xx"` or `"4xx"`. |
| "Backend response exceeded the size limit" | Shorten `WINDOW_DAYS` and rerun. |
| "callback time budget exhausted" or "execution deadline exceeded" | Rerun once unchanged. If it fails again, report it. |
| Other error | Report its `title`/`detail` verbatim. |

Done when there is a record for the final window, or a reported outcome.

## 2. Analyze

Build the analysis only from the record. Read it this way:

- **Normal level.** `baseline_rate_pct` is the Hostname's normal error rate. `baseline_source: "reference"` means it comes from the 7 days before the window, which is the reliable case. `"window"` means there wasn't enough earlier traffic, so it comes from the window itself. Then, if `baseline_unreliable` is `true`, most of the window is elevated and the baseline may already be the incident level. Say so.
- **Episodes.** Each one is a run of buckets above `spike_threshold_pct` with enough errors to rule out noise. For each, give:
  - `rise_began` (when the rate started climbing) and `start` (when it crossed the threshold). Use `rise_began` as the incident time.
  - `end`, or "still ongoing" if `ongoing_at_window_end`. A bucket that ends at "now" may be partial, because data lags about 10 minutes.
  - `error_rate_pct` and `peak_rate_pct`, each next to the baseline, plus `excess_errors`.
- **No episodes.** Say the error rate stayed within its normal range. Quote `totals.error_rate_pct` and the baseline. A high but steady rate isn't a spike: say it's the Hostname's normal level for the reference period, and give no judgment on whether that level is acceptable.
- **Response codes.** List the top codes per episode. A code missing from `response_codes` because of `response_codes_skipped` is unknown, not zero.
- **Did the errors reach origin?** `to_origin` per code and `errors_reaching_origin_pct` count origin requests made for the erroring requests. They can slightly exceed the error count, for example through retries.
  - Close to 100%: these requests were forwarded to origin. *Inference:* origin, or the connection to it, produced the errors.
  - Close to 0%: the edge answered without contacting origin. *Inference:* an edge-side decision or failure.
  - Always label these as inferences. The report doesn't show who generated the code.
- **Series.** `series.points` shows the shape: a sudden step, a ramp, or a short burst. Describe it in one sentence.

**Hard rules:**
- **Every claim needs a field.** Don't bring in outside knowledge about the Hostname, its origin or its traffic.
- **Label every inference** with *Inference:*.
- **No causes, no recommendations.** This skill says when and how much, not why.

## 3. Output

In this order:

1. **Headline:** one line: Hostname, window in UTC, and either "no spike" or the number of episodes, with the largest episode's rate against the baseline.
2. `## Episodes`: a table: rise began, start, end, error rate vs baseline, peak, top codes, reaching origin. Then one sentence on the shape of the series.
3. `## Next step`: if there is an episode, give the incident time (`rise_began`) and offer the change-history skill with `WINDOW_END` = `rise_began` + one bucket and `WINDOW_DAYS = 1`. If the user already asked what changed or why, run change-history now instead of offering it.
4. `## Spike record`: the final run's `result` as one fenced `json` block, copied exactly as the tool returned it.
