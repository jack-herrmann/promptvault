"""Remove secrets from prompt text before it is written anywhere.

Two layers: well-known token formats (always redacted) and natural-language
"my password is X" phrases (EN + DE), where X is only redacted when it looks
like a secret, so "the token is invalid" or "pwd is /Users/me" survive.
"""

import re

MARK = "«redacted:%s»"  # «redacted:kind»

_TOKENS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-|or-v1-)?[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}")),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]{16,}=*")),
]

# scheme://user:password@host -> keep scheme and user
_URL_CRED = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:)([^/\s@]+)(@)")

# NAME_WITH_SECRET_WORD = value
_ASSIGN = re.compile(
    r"\b([A-Za-z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE)[A-Za-z0-9_]*)(\s*[=:]\s*)([\"']?)([^\s\"']{6,})\3"
)

_KEYWORDS = (
    r"passwor[dt]|kennwort|passwd|passphrase|passcode|pwd|pw|pin|api[ _\-]?key|secret|token|zugangscode"
)
_VERBS = r"is|ist|lautet|was|war|=|:"
# keyword, up to 4 filler words, a verb, an optional opening quote, then the value
_PHRASE = re.compile(
    r"(?i)\b(?P<kw>" + _KEYWORDS + r")\b(?P<fill>(?:[ \t]+[^\s=:\"'“”„«»]+){0,4}?)"
    r"[ \t]*(?P<verb>\b(?:" + _VERBS + r")\b|[=:])[ \t]*(?P<q>[\"'“”„«]?)"
    r"(?P<val>[^\s\"'“”„»]+)"
)
_PASS = re.compile(r"(?i)\bpass\s*[:=]\s*(?P<q>[\"']?)(?P<val>[^\s\"']+)")

# Values that are almost never secrets ("the token is invalid", "password is wrong").
_COMMON = set(
    """invalid expired missing wrong correct required needed empty null none nil undefined set unset stored saved
    the a an not still same here there below above attached in on at of for to from my your our their this that it
    ok okay fine good bad weak strong short long new old reset changed different incorrect right true false yes no
    leaked exposed compromised visible hidden broken working fixed ready done gone available unavailable valid
    required. optional default secret password token key unknown blank generated rotated revoked encrypted hashed
    falsch richtig leer gesetzt abgelaufen ungültig gültig neu alt geändert""".split()
)
_TRAIL = ".,;!?)]}>"
_DICT_PATH = "/usr/share/dict/words"
_dict_words = None


def _is_dictionary_word(word):
    """Loaded lazily: only reached when a phrase matched with an all-lowercase value (rare)."""
    global _dict_words
    if _dict_words is None:
        try:
            with open(_DICT_PATH, encoding="utf-8", errors="ignore") as f:
                _dict_words = {w.strip().lower() for w in f}
        except OSError:
            _dict_words = set()
    return word in _dict_words


def _looks_secret(kw, val):
    v = val.rstrip(_TRAIL)
    if not v:
        return False
    low = v.lower()
    if low in _COMMON or low.startswith(("$", "%", "{", "<", "os.environ", "process.env", "env[", "«redacted")):
        return False
    if v.startswith(("/", "~", "./", "../")):  # paths (pwd is /Users/...)
        return False
    if kw.lower() in ("pin", "passcode", "zugangscode"):
        return bool(re.fullmatch(r"\d{4,10}", v)) or len(v) >= 6
    if len(v) < 6:
        return False
    # An ordinary English word ("token is generated") is not a secret.
    if v.isalpha() and v.islower() and _is_dictionary_word(low):
        return False
    return True


def redact(text):
    """Return (redacted_text, count, kinds)."""
    if not text:
        return text, 0, []
    kinds = []

    def sub_token(kind):
        def f(_m):
            kinds.append(kind)
            return MARK % kind
        return f

    for kind, rx in _TOKENS:
        text = rx.sub(sub_token(kind), text)

    def url_f(m):
        kinds.append("url-password")
        return m.group(1) + MARK % "password" + m.group(3)

    text = _URL_CRED.sub(url_f, text)

    def assign_f(m):
        val = m.group(4)
        if not _looks_secret("secret", val):
            return m.group(0)
        kinds.append("assignment")
        return m.group(1) + m.group(2) + m.group(3) + MARK % "secret" + m.group(3)

    text = _ASSIGN.sub(assign_f, text)

    def phrase_f(m):
        val = m.group("val")
        if not _looks_secret(m.group("kw"), val):
            return m.group(0)
        core = val.rstrip(_TRAIL)
        kinds.append("phrase")
        start = m.start("val") - m.start()
        whole = m.group(0)
        return whole[:start] + MARK % "password" + val[len(core):]

    text = _PHRASE.sub(phrase_f, text)

    def pass_f(m):
        val = m.group("val")
        if not _looks_secret("pass", val):
            return m.group(0)
        kinds.append("phrase")
        start = m.start("val") - m.start()
        core = val.rstrip(_TRAIL)
        return m.group(0)[:start] + MARK % "password" + val[len(core):]

    text = _PASS.sub(pass_f, text)
    return text, len(kinds), sorted(set(kinds))
