"""Every filesystem location PROMPTVAULT uses. PROMPTVAULT_* env vars override them (tests, spikes)."""

import os

HOME = os.path.expanduser("~")


def _env(name, default):
    return os.environ.get("PROMPTVAULT_" + name) or default


# The checkout holds the vault and database. The installed runtime copy finds it through the SOURCE
# file that `promptvault install` writes next to the package; a checkout finds itself.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _project_dir():
    try:
        with open(os.path.join(_HERE, "SOURCE")) as f:
            return f.read().strip() or _HERE
    except OSError:
        return _HERE


PROJECT_DIR = _env("PROJECT", "") or _project_dir()
VAULT_DIR = _env("VAULT", PROJECT_DIR + "/vault")
# ".noindex" keeps Spotlight out of the canonical database.
DATA_DIR = _env("DATA", PROJECT_DIR + "/data.noindex")
DB_PATH = DATA_DIR + "/promptvault.db"

# The runtime copy and the state live outside ~/Documents so hooks keep working
# from contexts without Documents access, and survive renaming the repo.
RUNTIME_DIR = _env("RUNTIME", HOME + "/.local/share/promptvault")
STATE_DIR = _env("STATE", HOME + "/.local/state/promptvault")
SPOOL_DIR = STATE_DIR + "/spool"
FAILED_DIR = SPOOL_DIR + "/failed"
LOCK_PATH = STATE_DIR + "/daemon.lock"
LOG_PATH = STATE_DIR + "/daemon.log"
HOOK_STATUS_PATH = STATE_DIR + "/hook.status"
PAUSE_PATH = STATE_DIR + "/pause"
CONFIG_PATH = STATE_DIR + "/config.json"
SPAWNS_PATH = STATE_DIR + "/spawns.json"
STOP_REQUEST_PATH = STATE_DIR + "/stop.request"

ASIDE_DB = _env("ASIDE_DB", HOME + "/.aside/u/0/state.db")
CC_HISTORY = _env("CC_HISTORY", HOME + "/.claude/history.jsonl")
CC_SETTINGS = _env("CC_SETTINGS", HOME + "/.claude/settings.json")

# Command Line Tools python, not the /usr/bin shim (which breaks when the Xcode licence needs re-accepting).
PYTHON = _env("PYTHON", "/Library/Developer/CommandLineTools/usr/bin/python3")
CLI_SHIM = _env("CLI_SHIM", HOME + "/.local/bin/promptvault")


def hook_command():
    """Shell-form hook command: Cursor ignores exec-form args. Never prints, always exits 0."""
    return "%s -I -S %s/pv_hook.py >/dev/null 2>&1 || true" % (PYTHON, RUNTIME_DIR)


def env_overrides():
    """PROMPTVAULT_* variables to pass on to a spawned daemon."""
    return {k: v for k, v in os.environ.items() if k.startswith("PROMPTVAULT_") and k != "PROMPTVAULT_SKIP"}
