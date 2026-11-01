"""Install / uninstall: runtime copy, CLI shim, and our entries in ~/.claude/settings.json.

Settings edits touch only entries whose command runs our pv_hook.py, are written the way
Claude Code writes the file (indent 2, UTF-8, trailing newline, atomic rename), and are
verified by re-reading.
"""

import copy
import datetime
import json
import os
import shutil
import stat

from promptvault import paths

EVENTS = ("UserPromptSubmit", "Stop", "StopFailure", "SessionEnd")
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY_SCRIPTS = {
    "pv_hook.py": "from promptvault.hook import main\nmain()\n",
    "pvd.py": "from promptvault.daemon import main\nsys.exit(main())\n",
    "pv_cli.py": "from promptvault.cli import main\nsys.exit(main(sys.argv[1:]))\n",
}
_ENTRY_HEAD = ("import os\nimport sys\n\n"
               "# -I leaves the script's own folder off sys.path; add it explicitly (0700, owned by you).\n"
               "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n")


def marker():
    return os.path.join(paths.RUNTIME_DIR, "pv_hook.py")


def is_ours(hook):
    return isinstance(hook, dict) and marker() in str(hook.get("command", ""))


def our_entry():
    return {"type": "command", "command": paths.hook_command(), "async": True, "timeout": 2}


def add_hooks(settings):
    s = copy.deepcopy(settings)
    hooks = s.setdefault("hooks", {})
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        if not any(is_ours(h) for g in groups for h in (g.get("hooks") or [])):
            groups.append({"hooks": [our_entry()]})
    return s


def remove_hooks(settings):
    s = copy.deepcopy(settings)
    hooks = s.get("hooks")
    if not isinstance(hooks, dict):
        return s
    for ev in list(hooks):
        groups = hooks.get(ev)
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            hs = [h for h in (g.get("hooks") or []) if not is_ours(h)]
            if hs:
                g = dict(g)
                g["hooks"] = hs
                kept.append(g)
            elif not (g.get("hooks") or []):
                kept.append(g)  # someone else's empty group: leave it alone
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if not hooks:
        del s["hooks"]
    return s


def has_our_hooks(settings):
    hooks = settings.get("hooks") or {}
    return all(any(is_ours(h) for g in hooks.get(ev) or [] for h in (g.get("hooks") or [])) for ev in EVENTS)


def _dump(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _read_settings(path):
    if not os.path.exists(path):
        return {}, None
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    return (json.loads(raw) if raw.strip() else {}), raw


def _write_atomic(path, text, mode):
    tmp = "%s.promptvault-tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def edit_settings(transform, check, path=None, backup=True, attempts=3):
    """Read-modify-write with verification; retries if something else rewrote the file meanwhile."""
    path = path or paths.CC_SETTINGS
    if os.path.islink(path):
        raise RuntimeError("%s is a symlink; refusing to replace it" % path)
    for _ in range(attempts):
        before, raw = _read_settings(path)
        after = transform(before)
        if after == before:
            return before, after, None
        bak = None
        if backup and raw is not None:
            bak = "%s.bak-%s-promptvault" % (path, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
            with open(bak, "w", encoding="utf-8") as f:
                f.write(raw)
        mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
        _write_atomic(path, _dump(after), mode)
        now, _ = _read_settings(path)
        if check(before, now):
            return before, now, bak
    raise RuntimeError("settings.json kept changing underneath us; nothing left half-done, try again")


def source_dir():
    """The repo's package, even when this code runs from the installed runtime copy."""
    repo_pkg = os.path.join(paths.PROJECT_DIR, "promptvault")
    return repo_pkg if os.path.isfile(os.path.join(repo_pkg, "install.py")) else PKG_DIR


def install_runtime():
    os.makedirs(paths.RUNTIME_DIR, mode=0o700, exist_ok=True)
    os.chmod(paths.RUNTIME_DIR, 0o700)
    src_pkg = source_dir()
    dest = os.path.join(paths.RUNTIME_DIR, "promptvault")
    if os.path.realpath(src_pkg) == os.path.realpath(dest):
        raise RuntimeError("no repo found at %s; nothing to install from" % paths.PROJECT_DIR)
    tmp = dest + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for name in sorted(os.listdir(src_pkg)):
        if name.endswith(".py"):
            shutil.copy2(os.path.join(src_pkg, name), os.path.join(tmp, name))
    old = dest + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(dest):
        os.replace(dest, old)
    os.replace(tmp, dest)
    shutil.rmtree(old, ignore_errors=True)
    for name, body in ENTRY_SCRIPTS.items():
        with open(os.path.join(paths.RUNTIME_DIR, name), "w") as f:
            f.write(_ENTRY_HEAD + body)
    # Atomic: hooks and the daemon read SOURCE to find the vault (see paths.py).
    _write_atomic(os.path.join(paths.RUNTIME_DIR, "SOURCE"), os.path.dirname(src_pkg) + "\n", 0o644)
    os.makedirs(os.path.dirname(paths.CLI_SHIM), exist_ok=True)
    with open(paths.CLI_SHIM, "w") as f:
        f.write('#!/bin/sh\nexec %s -I %s/pv_cli.py "$@"\n' % (paths.PYTHON, paths.RUNTIME_DIR))
    os.chmod(paths.CLI_SHIM, 0o755)


def ensure_state():
    for d in (paths.STATE_DIR, paths.SPOOL_DIR):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    if not os.path.exists(paths.CONFIG_PATH):
        from promptvault.daemon import DEFAULT_CONFIG

        with open(paths.CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
