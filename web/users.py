"""User store — SQLite on the Railway volume, scrypt-hashed passwords.

Deliberately a SEPARATE database file from research.db. The research DB gets pulled off
the volume with `railway volume files download`; if users lived in it, every download
would also export the password hashes.

Passwords are never stored. scrypt (stdlib hashlib, no dependency) with a per-user random
salt; the stored string carries its own parameters so cost can be raised later without
invalidating existing rows.

CLI:
    python -m web.users list
    python -m web.users add <username> <password> [--role admin]
    python -m web.users passwd <username> <new-password>
    python -m web.users delete <username>
"""
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

# Tuned so a single verification costs ~100ms on modest hardware — slow enough that
# offline brute force is expensive, fast enough for interactive login.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


def db_path() -> str:
    """Defaults next to research.db so both live on the same mounted volume."""
    explicit = os.getenv("USERS_DB_PATH")
    if explicit:
        return explicit
    research = os.getenv("RESEARCH_DB_PATH")
    if research:
        return os.path.join(os.path.dirname(research) or ".", "users.db")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "baseball", "data", "history", "users.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or db_path()
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT    NOT NULL,   -- scrypt$n$r$p$salt$hash — never a plaintext
            role          TEXT    NOT NULL DEFAULT 'user',
            created_at    TEXT    NOT NULL
        )""")
    conn.commit()
    return conn


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Parameters come from the stored string, so rows
    hashed under older settings keep working after _N is raised."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p),
                            dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def create_user(username: str, password: str, role: str = "user",
                path: str | None = None) -> bool:
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (username, hash_password(password), role,
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False           # username taken
    finally:
        conn.close()


def get_user(username: str, path: str | None = None) -> dict | None:
    conn = connect(path)
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users(path: str | None = None) -> list[dict]:
    conn = connect(path)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY id")]
    conn.close()
    return rows


def set_password(username: str, password: str, path: str | None = None) -> bool:
    conn = connect(path)
    cur = conn.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                       (hash_password(password), username))
    conn.commit(); conn.close()
    return cur.rowcount > 0


def delete_user(username: str, path: str | None = None) -> bool:
    conn = connect(path)
    cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit(); conn.close()
    return cur.rowcount > 0


def authenticate(username: str, password: str, path: str | None = None) -> dict | None:
    """Returns the user row on success, else None.

    A missing username still runs a full scrypt verification against a dummy hash, so
    the response time does not reveal whether an account exists.
    """
    user = get_user(username, path)
    if user is None:
        # Exactly ONE scrypt call, same as the found-user path. Hashing a dummy here
        # instead would cost two and make a missing account measurably slower — which
        # is a username oracle. _DUMMY_HASH is precomputed once at import.
        verify_password(password, _DUMMY_HASH)
        return None
    return user if verify_password(password, user["password_hash"]) else None


# Precomputed once so the missing-user path costs exactly one verification.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def bootstrap_from_env(path: str | None = None) -> int:
    """Seed the store on first run so a fresh deploy is never locked out.

    Reads the same DASH_USERS / DASH_PASS variables the env-based auth used, and only
    inserts usernames that do not already exist — so it is safe to leave the variables
    set, and it never overwrites a password changed through the CLI.
    """
    created = 0
    entries = []
    for entry in (os.getenv("DASH_USERS") or "").split(","):
        parts = [x.strip() for x in entry.split(":")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            entries.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "user"))
    legacy_pass = os.getenv("DASH_PASS")
    if legacy_pass:
        entries.append((os.getenv("DASH_USER") or "kai", legacy_pass, "admin"))
    for name, pw, role in entries:
        if get_user(name, path) is None and create_user(name, pw, role, path):
            created += 1
    return created


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "list":
        rows = list_users()
        if not rows:
            print("no users — run: python -m web.users add <name> <password> --role admin")
        for u in rows:
            print(f"  {u['id']:>3}  {u['username']:<20} {u['role']:<8} {u['created_at'][:19]}")
    elif cmd == "add" and len(args) >= 2:
        role = "admin" if "--role" in args and "admin" in args else "user"
        ok = create_user(args[0], args[1], role)
        print(f"{'created' if ok else 'FAILED — username taken'}: {args[0]} ({role})")
        return 0 if ok else 1
    elif cmd == "passwd" and len(args) >= 2:
        print("updated" if set_password(args[0], args[1]) else "no such user")
    elif cmd == "delete" and args:
        print("deleted" if delete_user(args[0]) else "no such user")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
