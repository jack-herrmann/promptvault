# PROMPTVAULT

Every prompt you type into an agent, saved the moment you send it, as notes in an Obsidian vault (`vault/`). Phase 1 is capture; Phase 2 (a prompt coach that reads `data.noindex/promptvault.db`) comes later.

## What gets captured

| Source | How | Outcome signals |
|---|---|---|
| Claude Code | `UserPromptSubmit` / `Stop` / `StopFailure` / `SessionEnd` hooks in `~/.claude/settings.json` (async: zero latency, nothing reaches the model) | completed, interrupted (Esc), error, files changed, tool calls, likely correction |
| Cursor | Cursor runs Claude Code's hooks itself (synchronously, ~50 ms) | completed / interrupted / error |
| Aside | read-only polling of `~/.aside/u/0/state.db` every 2 s | completed / interrupted / error, files |
| Missed prompts | `~/.claude/history.jsonl` is checked every minute; anything the hooks missed is added and flagged | none |

Your own prompts go to `vault/sessions/<agent>/<month>/`. Sessions with only agent-written prompts (`claude -p`, `claude --bg` sessions, Aside CLI/sub-agent/routine tasks) go to `vault/agent-written/`. Agent messages inside your sessions show as collapsed `#agent-written` callouts: Claude Code peer messages, and messages that agents post into an Aside chat you started (`aside session resume`, other Aside chats). Aside stores those as a list of text parts starting with a `[Sender]` tag, while text you type is a plain string. Task notifications and other harness turns are stored but not shown. Aside incognito sessions are never stored.

**Not captured:** web chats (claude.ai, ChatGPT), Codex, and Claude Code sessions started with `--bare` or `disableAllHooks`.

## Privacy

- Secrets are redacted before anything is written: API-key and token formats, private keys, URL passwords, and "my password is …" phrases in English and German. You'll see `«redacted:…»` in their place.
- Everything stays on this Mac. Nothing is uploaded, and the hook never prints anything.
- `vault/` and `data.noindex/` are git-ignored and Cursor-ignored. `data.noindex/` is excluded from Spotlight; the vault is not, so notes stay searchable locally.
- `promptvault pause 1h` before typing something private (`resume` to continue), or `promptvault forget <session-id>` afterwards.
- `PROMPTVAULT_SKIP=1` in a process's environment skips capture for that process (use it for scripts and the future coach).

## Commands

```
promptvault doctor            # health check (hooks, daemon, spool, last capture per agent)
promptvault tail [-n 20]      # recent captures, metadata only
promptvault pause [30m|2h|indef] / resume
promptvault forget <session-id-prefix>
promptvault rebuild [session] # re-render notes from the database
promptvault start | stop | status
promptvault retry-failed
promptvault install           # after editing code here: copies the runtime, restarts the daemon
promptvault uninstall         # removes our hooks, daemon, runtime and spool; keeps vault and database
```

The daemon starts automatically from the first Claude Code prompt after a reboot. Aside prompts sent while it was down are caught up from Aside's database.

## How it works

```
hook (≈50 ms, silent, exit 0) ─► ~/.local/state/promptvault/spool/*.json ─► promptvaultd ─► data.noindex/promptvault.db ─► vault/*.md
Aside state.db (read-only) ─────────────────────────────────────────────────────┘
```

- **Runtime:** `~/.local/share/promptvault/` (installed copy; hooks point here).
- **State:** `~/.local/state/promptvault/` (spool, lock, log without prompt text, pause, config).
- **Config:** `~/.local/state/promptvault/config.json`:
  - `exclude_cwd_prefixes`: folders to never capture.
  - `agent_cwd_patterns`: text that marks a session's folder as an agent's, e.g. the workspace folder of a tool that runs agents for you. Those sessions count as agent-written.
  - `agent_text_prefixes`: openings that mark a message as an agent's even when it arrives looking typed, e.g. `"[my-relay "` for a tool that forwards messages between agents.
- **Notes** are regenerated from the database, so edits inside them are overwritten. Annotate in your own notes and link to a prompt as `[[note name#^p3]]`.

## Known limits

- `claude -p` runs: the async `Stop` hook is cut off at exit, so their outcome shows as "ended without a result". These are agent-written sessions anyway.
- The daemon only starts from apps that already have Documents access (iTerm2, Terminal, Cursor, VS Code, Aside). Inside tmux it doesn't, so events wait in the spool until it is started.
- Cursor capture is verified against Cursor's code, not live. Check it by sending one Cursor prompt and running `promptvault tail`.

## Development

`python3 -m unittest discover -s tests -t .` (CLT Python 3.9, stdlib only), then `promptvault install`. Read `CLAUDE.md` before working in this repo with an agent.
