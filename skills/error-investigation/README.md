# Error Investigation

A Claude Code and Codex skill that turns an Akamai **Reference ID** (the `Reference #18.xxxxxxxx.xxxxxxxxxx.xxxxxxxx` line at the bottom of an Akamai error page) into:

1. **A case record:** JSON holding every fact Akamai Technical Support (Akatec) needs to trace the failed request end to end, including the edge logs. Attach it when you open an Akatec case.
2. **An analysis:** where the decision was made, what fired, who the client was, and a one-line verdict (*Akamai configuration*, *Origin failure*, *Akamai platform issue* or *Inconclusive*). Every claim cites a field in the case record.

The skill only diagnoses. Every call it makes is a read, and it changes nothing on the Account.

## Requirements

- **The Akamai MCP Server** connected to Claude Code or Codex, on the Account that served the failed request.
- **Edge Diagnostics** (error translation) access for the connection's API client.
- *Optional:* read access to **App & API Protector**. It adds the Security Configuration, policy and rule names to WAF blocks. Without it, those blocks still carry the IDs.

You don't need to install anything locally. `scripts/case_record.py` runs inside the Akamai MCP Server's own `execute` sandbox. If the server exposes its tools directly instead, the agent follows the script as a spec.

## Install

A skill is a folder containing a `SKILL.md`. Put this folder, `error-investigation/`, where your agent looks for skills. A symlink to a clone of this repo keeps the skill updated with `git pull`.

```sh
git clone https://github.com/pmedranos/akamai-mcp.git
cd akamai-mcp
```

### Claude Code

Pick one scope:

```sh
# Every project, for your user only
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/error-investigation" ~/.claude/skills/error-investigation

# One project, shared with everyone who works in it
mkdir -p /path/to/project/.claude/skills
ln -s "$PWD/skills/error-investigation" /path/to/project/.claude/skills/error-investigation
```

Use `cp -R` instead of `ln -s` if you don't want a link back to the clone. Restart Claude Code, or start a new session, then check that `/error-investigation` appears in the `/` menu.

### Codex

```sh
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/error-investigation" ~/.codex/skills/error-investigation
```

Restart Codex. `agents/openai.yaml` supplies the display name, the default prompt and the MCP dependency.

### Connect the Akamai MCP Server

The Akamai MCP Server is in private beta, so ask your Akamai contact for its URL and credentials. Then register it as a remote MCP server:

- **Claude Code:** `claude mcp add --transport http akamai-mcp <server-url>`. Run `/mcp` to check that it's connected.
- **Codex:** add it to `~/.codex/config.toml`:

  ```toml
  [mcp_servers.akamai-mcp]
  url = "<server-url>"
  ```

## Usage

```text
/error-investigation Reference #18.xxxxxxxx.xxxxxxxxxx.xxxxxxxx
```

- **Production is the default.** If you reproduced the error on Akamai's staging network, add the word `staging`: `/error-investigation staging Reference #18.…`. The skill never guesses the network. For WAF blocks, it checks the stated network against the Security Configuration version that fired, and tells you when they don't match.
- **Several IDs at once** work. The analysis compares only the fields that appear in every record.
- **In Codex**, mention `$error-investigation` and paste the Reference ID.

Reference IDs translate for **48 hours** only, so run the skill soon after the error.

## Outcomes other than a case record

| Outcome | Meaning | What to do |
|---|---|---|
| `EXPIRED` | The ID is older than 48 h. | Reproduce the error and use the new Reference ID. |
| `NO_LOGS` (`not.found.logs.rotated`) | The translation finished, but Akamai's logs weren't readable yet. This is common for very recent errors. | Run the skill again later with the same ID while it's within 48 h. |
| `NO_LOGS` (another type) | Edge Diagnostics found no log lines for the request. | Reproduce the error and use the new Reference ID. |
| `IN_PROGRESS` | The translation is still running. | The skill polls again; it never restarts the translation. |
| `FAILURE`/`REJECTED` with 403 | Another Account served the request, or the API client lacks Edge Diagnostics access. | Connect the right Account, or grant Edge Diagnostics access. |

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | The instructions the agent follows: gather the facts, analyze them, format the output. |
| `scripts/case_record.py` | Builds the case record inside the Akamai MCP Server's `execute` sandbox, in two runs per ID: start the translation, then build the record. Only its three marked input lines are edited per run. |
| `agents/openai.yaml` | Codex metadata: display name, default prompt, MCP dependency. |
