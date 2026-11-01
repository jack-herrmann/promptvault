# PROMPTVAULT: rules for agents working in this repo

- Never open files under `vault/` with Read/Edit/Write (deny them in the git-ignored `.claude/settings.local.json`). Claude Code re-sends a diff of any file it has read whenever that file changes, and the daemon rewrites notes constantly, so reading a note would feed the user's prompts into your context every turn.
- Verify captures with `promptvault tail`, `promptvault doctor`, or `sqlite3 data.noindex/promptvault.db` metadata queries. Never print prompt text.
- Tests use synthetic fixtures only (`tests/`); never copy real prompts, transcripts or Aside rows into the repo.
- After changing code under `promptvault/`, run `python3 -m unittest discover -s tests -t .` and then `promptvault install` (copies the runtime to ~/.local/share/promptvault and restarts the daemon).
- Python: `/Library/Developer/CommandLineTools/usr/bin/python3` (3.9). Stdlib only.
