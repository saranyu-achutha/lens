"""Named client logins (2026-11-12): read-only client accounts with their own username and password, e.g. for Times Now.

A named client sees exactly what the shared client login sees (Dashboard, Management, Market share, Platforms, Reports,
Daily data, Songs; read-only) and can be removed on its own. Passwords are never stored: `.env` keeps only a salted
PBKDF2-SHA256 hash per user in CLIENT_LOGINS ("user:pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>", comma-separated).
A checked login is remembered in memory (by a SHA-256 of user and password) so pages don't re-run the slow hash on
every request.

  python3 -m lens.logins add <username>      # creates the account, prints a strong generated password once
  python3 -m lens.logins reset <username>    # a new password for an existing account (printed once)
  python3 -m lens.logins remove <username>
  python3 -m lens.logins list
The running Console picks changes up within a minute (no restart).
"""
import argparse
import hashlib
import hmac
import re
import secrets
import string
import sys
import threading
import time

from . import config

KEY = "CLIENT_LOGINS"
ITERATIONS = 240_000
USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
_CACHE, _LOCK = {"at": 0.0, "users": {}, "ok": {}}, threading.Lock()


def make_hash(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${h.hex()}"


def check_hash(password, stored):
    try:
        algo, it, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


def users():
    """{username: hash} from .env (re-read at most once a minute)."""
    with _LOCK:
        if time.time() - _CACHE["at"] > 60:
            raw = config.read_secrets().get(KEY) or ""
            parsed = {}
            for part in raw.split(","):
                if ":" in part:
                    u, h = part.split(":", 1)
                    if USER_RE.match(u.strip()):
                        parsed[u.strip()] = h.strip()
            if parsed != _CACHE["users"]:
                _CACHE["ok"] = {}  # a changed or removed account must not stay valid from the memory
            _CACHE["users"], _CACHE["at"] = parsed, time.time()
        return dict(_CACHE["users"])


def verify(user, password):
    """True for a named client account with the right password."""
    stored = users().get(user)
    if not stored or not password:
        return False
    key = hashlib.sha256(f"{user}\0{password}\0{stored}".encode()).hexdigest()
    with _LOCK:
        if _CACHE["ok"].get(key):
            return True
    ok = check_hash(password, stored)
    if ok:
        with _LOCK:
            _CACHE["ok"][key] = True
    return ok


def generate_password(length=20):
    """A strong password: letters, digits and a few symbols, at least one of each kind."""
    alphabet = string.ascii_letters + string.digits + "@#%+=-_"
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p)
                and any(c in "@#%+=-_" for c in p)):
            return p


def _save(accounts):
    config.set_env(KEY, ",".join(f"{u}:{h}" for u, h in sorted(accounts.items())))
    with _LOCK:
        _CACHE["at"] = 0.0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lens.logins", description="Named read-only client logins")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("add", "reset", "remove"):
        sub.add_parser(c).add_argument("username")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    accounts = users()
    if a.cmd == "list":
        print("\n".join(sorted(accounts)) or "(no named client logins)")
        return
    u = a.username.strip().lower()
    if not USER_RE.match(u):
        sys.exit("username: 3-32 characters, lower-case letters, digits, '.', '_' or '-', starting with a letter or digit")
    secrets_now = config.read_secrets()
    if u == (secrets_now.get("DEVELOPER_USERNAME") or "").lower():
        sys.exit("that is the developer login's username; choose another")
    if a.cmd == "remove":
        if u not in accounts:
            sys.exit(f"no client login {u!r}")
        accounts.pop(u)
        _save(accounts)
        print(f"removed client login {u!r}")
        return
    if a.cmd == "add" and u in accounts:
        sys.exit(f"client login {u!r} exists already (use reset for a new password)")
    if a.cmd == "reset" and u not in accounts:
        sys.exit(f"no client login {u!r}")
    pw = generate_password()
    accounts[u] = make_hash(pw)
    _save(accounts)
    print(f"client login {'created' if a.cmd == 'add' else 'reset'}: username {u}  password {pw}")
    print("The password is shown only now (only its hash is stored). Access: client pages, read-only.")


if __name__ == "__main__":
    main()
