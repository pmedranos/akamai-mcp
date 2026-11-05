---
name: error-investigation
description: Translate an Akamai Reference ID (`Reference #18.xxxx…` from an Akamai error page) or Global Request Number into an Akatec-ready case record (JSON facts plus edge logs) and an evidence-based analysis, via the Akamai MCP Server. Use when the user pastes a Reference ID or an Akamai error page, or asks why a request got an Akamai error.
---

# Error Investigation

The user has a Reference ID from an Akamai error page. For each ID, produce:

1. A **case record**: JSON holding every fact Akamai Technical Support (Akatec) needs to trace the failed request end to end, including the edge logs. The user attaches it when opening an Akatec case, so Akatec can start work without asking for more information.
2. An **analysis**: what happened, where the decision was made, and a verdict, with every claim tied to the case record.

This skill is diagnosis only. Every call is a read; it changes nothing on the Account.

## Requirements

Check these when a call fails:
- The Akamai MCP Server connected to Claude Code or Codex, on the Account that served the request.
- The connection's API client has access to **Edge Diagnostics** (error translation).
- *Optional:* read access to **App & API Protector**. It adds the Security Configuration, policy and rule names to WAF blocks. Without it, those blocks still carry the IDs.
- Nothing to install locally. The script in `scripts/` runs inside the Akamai MCP Server's own sandbox, or it isn't used at all (see 1B).

## 1. Gather the facts

**No Reference ID?** If the user's message doesn't contain a Reference ID, ask for one and stop. Tell them it's at the bottom of the Akamai error page and looks like `Reference #18.xxxxxxxx.xxxxxxxxxx.xxxxxxxx`. Never use an ID from this file, the script, or an earlier conversation the user didn't point to.

How you call Akamai depends on how the Akamai MCP Server is exposed in this session.

**A. The server has an `execute` tool** (it runs Python scripts that call `call_tool`). Read `scripts/case_record.py`, which sits next to this file, and run it twice per Reference ID. Each time, pass the whole file as `code`.

The script has a marked block at the top with two lines: `REFERENCE_ID` and `REQUEST_ID`. **Change only those two lines.** Don't remove comments, shorten functions, or edit anything else.

1. **Run 1:** set `REFERENCE_ID` to the Reference ID exactly as the user gave it, `Reference #` included. Leave `REQUEST_ID = None`.
   - The script normalizes the ID, checks its age, starts the translation and polls for about 20 s.
   - It returns `status` and `request_id`.
2. **Run 2:** keep the same `REFERENCE_ID`, and set `REQUEST_ID` to the `request_id` number from run 1.
   - The script fetches the translation, runs the WAF lookups and returns the finished case record.
   - If it returns `status` `IN_PROGRESS`, run run 2 again unchanged. Never go back to run 1 for the same ID.

The two runs exist because one run that does both exceeds `execute`'s time limit.

**B. The server exposes tools directly** (`ediag_translate_err_str`, `ediag_check_translate_err_status`, …): use `scripts/case_record.py` as the spec, and follow it step by step.
1. **Normalize the ID:** decode HTML entities, strip `Reference #`, and lowercase it.
2. **Check its age:** the third dot-field is the Unix time of the error. If it's older than 48 h, report it *expired* and stop.
3. **Translate:** call `ediag_translate_err_str` with `error_code` and `trace_forward_logs: true`.
4. **Poll:** while the status is `IN_PROGRESS`, wait `retryAfter` seconds, then call `ediag_check_translate_err_status` with `request_id`.
5. **Look up the WAF details** if `wafDetails` is present and the `aap_manage_*` tools exist. Use the same three `"operation": "get"` calls as `waf_block()`. If those tools are missing, keep the IDs parsed from `wafDetails`/`wafDetailsUrl` and leave the names `null`.
6. **Build the record** with exactly the field mapping in `case_record()`, `edge_logs()` and `decode_status()`.

**Guardrail:** call the `aap_manage_*` tools only with `"operation": "get"`, because they can also create and update.

Handle each outcome:

| Outcome | Action |
|---|---|
| Case record | Go to step 2. |
| `EXPIRED`, or `REJECTED` with `expired.field.value` | Reference IDs translate for 48 h only. Report the age and ask for a fresh ID: reproduce the error and copy the new Reference ID. |
| `NO_REFERENCE_ID` | `REFERENCE_ID` was left as the placeholder. Ask the user for a Reference ID. |
| `IN_PROGRESS` after polling | Poll `ediag_check_translate_err_status` with the `request_id` again (with `execute`: rerun run 2), then build the record. Don't restart the translation. |
| `FAILURE`/`REJECTED` with 403 | Give both possible causes: the request was served by a different Account than the connected one, or the API client lacks Edge Diagnostics access. Ask the user which applies. |
| Other error | Report its `title`/`detail` verbatim. |

Done when every ID has a case record or a reported outcome.

## 2. Analyze

Build the analysis only from the case record. Work through these lenses and keep the ones the evidence speaks to:

- **Where the decision was made.**
  - Edge log lines with `logType` `r` only, no `forwardIp`, and a `timeTaken` of a few ms mean the edge answered by itself.
  - `f` lines or a `forwardIp` mean origin was contacted. Read `forwardErrors`, `timeTaken` and `turnaroundTime` to see how origin behaved.
  - **An error on the forward hop isn't automatically origin's fault.** A TLS or certificate failure between edge and origin, such as an untrusted certificate, a certificate that isn't pinned, or a Common Name mismatch, is judged against the property's own `origin` settings: `verificationMode`, `customValidCnValues`, `originCertsToHonor`, the pinned `customCertificates`/`customCertificateAuthorities` and `originSni`. The error can come from either side: origin serves a wrong or expired certificate, or the property doesn't list a certificate or CN that origin legitimately serves. The case record doesn't include those settings, so say which side is unknown and why.
- **What fired.**
  - Read `failure_code` and `failure_reason`, and `object_status_decoded` for detail.
  - For a WAF block, read `rule_conditions` literally and state what the rule matches. Is it narrow (a specific header, IP or pattern) or a blanket match, such as `/*` on a list of hosts?
  - If the rule is a blanket block on hostnames that serve real traffic, legitimate traffic must avoid it some other way: another policy, an exception, or an allow list. Say this, and label it an inference.
- **Who the client was.** Use `user_agent`, `method`, `client_location` and whether `referer`/`cookie` appear in the edge logs. Does this client fit what the hostname and path are for? For example, a desktop browser calling a console API with no cookie looks like a manual test, not a real user. If `request_time` is only minutes before `checked_at_epoch`, it may be the user's own reproduction.
- **Change context.**
  - For a WAF block, read `waf.served_by_network` first. It says which Akamai network evaluated the request, and its `basis` says why.
    - `production`: the request ran on the production network. A different staging version didn't evaluate it and can't explain it. Mention the staging version only as "a newer version exists on staging"; don't say what it changes.
    - `staging`: the request ran on the staging network, so it tested the staged version, not what real users get.
    - `unknown`: say so and quote the `basis`. Don't guess the network.
  - Then compare `security_config_version` with the production and staging versions. All three equal means no pending security change.
  - For other errors, say whether the record holds any change evidence.

Write each point as **fact (cite the field) → what it means**.

**Hard rules for the analysis:**
- **Every claim needs a field.** If no field in this case record supports a claim, leave it out. Don't bring in outside knowledge about the Account, its IP ranges, what's "typical", or why someone configured a rule.
- **Label every inference.** Start any sentence that goes beyond what a field literally says with *Inference:*. That includes conclusions about intent, such as "the rule is deliberate".
- **Versions say only what they say.** Equal versions mean no security change is pending. They don't show how long the rule has been live or when it was last changed. Different versions mean a newer version exists. They don't show what changed or whether it touches this rule.
- **With several IDs, compare only fields that exist in the records,** for example the same `client_ip` and `rule_id` but a different `edge_server_ip`.

**Verdict:** one line, in one of these categories:
- *Akamai configuration* (the Account's own Akamai configuration produced the error, whether intended, like a WAF rule, or by mistake, like origin certificate settings that don't match what origin serves. Don't guess which: intent isn't in the record.)
- *Origin failure* (origin itself refused, timed out, returned an error or served a bad certificate, and nothing in the record points at the property's settings for reaching it)
- *Akamai platform issue* (the evidence points at the edge itself)
- *Inconclusive* (name the missing fact). For a TLS or certificate error on the forward hop, this is the default: the missing fact is the property's origin certificate settings.

Choose *Akamai platform issue* only when the record shows the edge itself failing in a way that neither the configuration nor origin explains. A WAF block, a rule the Account configured, or an error that origin returned is never a platform issue.

Add one sentence saying when the verdict would change. For example: "This becomes a real problem only if a legitimate console client hits this rule."

Stop at the verdict. Leave out recommendations, configuration changes and question lists. One request isn't enough evidence for a configuration change.

Done when every point cites a field and the verdict names exactly one category.

## 3. Output

For each Reference ID, in this order:

1. `## Case record`: the case record as one fenced `json` block, ready to attach to an Akatec case.
   - **Copy the `result` exactly as the tool returned it.** Don't retype, re-indent, reorder, shorten, translate or "fix" any key or value, including the long `object_status_decoded` descriptions. Akatec compares it against its own logs, so any change, even an accidentally repeated phrase, makes the record unreliable.
   - Keep every field, including `client_ip`, `user_agent` and `edge_logs`. Akatec uses them to trace the request end to end.
2. `## Analysis`: a one-line headline, the numbered points, then **Verdict**.

After that, list any IDs that couldn't be translated, and why.
