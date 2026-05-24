"""SQLite helpers for Clipscash."""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from flask import g

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "clipscash.db"
SCHEMA_PATH = ROOT / "schema.sql"


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_e=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def migrate() -> None:
    """Apply additive schema changes idempotently."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "banned" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
    if "banned_reason" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN banned_reason TEXT")
    if "banned_at" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN banned_at TEXT")
    if "brand_id" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN brand_id INTEGER REFERENCES users(id)")
        # Backfill: assign existing creators round-robin to existing brands.
        brand_ids = [r[0] for r in cur.execute(
            "SELECT id FROM users WHERE role='brand' ORDER BY id").fetchall()]
        creators = [r[0] for r in cur.execute(
            "SELECT id FROM users WHERE role='creator' AND brand_id IS NULL ORDER BY id").fetchall()]
        if brand_ids and creators:
            for i, cid in enumerate(creators):
                cur.execute("UPDATE users SET brand_id=? WHERE id=?",
                            (brand_ids[i % len(brand_ids)], cid))

    # Submissions: share_token column
    sub_cols = {r[1] for r in cur.execute("PRAGMA table_info(submissions)").fetchall()}
    if "share_token" not in sub_cols:
        cur.execute("ALTER TABLE submissions ADD COLUMN share_token TEXT")
        # Backfill share tokens for existing submissions
        import secrets as _secrets
        for r in cur.execute("SELECT id FROM submissions WHERE share_token IS NULL").fetchall():
            cur.execute("UPDATE submissions SET share_token=? WHERE id=?",
                        (_secrets.token_urlsafe(8), r[0]))
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_share_token ON submissions(share_token)")

    # Note: SQLite ALTER cannot drop CHECK constraints, so the role check
    # (creator/brand/admin) might not include 'fan' for legacy DBs. To allow
    # fan registration on old DBs, we rebuild the users table when needed.
    role_check = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if role_check and "'fan'" not in role_check[0]:
        new_users_sql = """
        CREATE TABLE users_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT UNIQUE NOT NULL,
            password_hash   TEXT NOT NULL,
            name            TEXT NOT NULL,
            role            TEXT NOT NULL CHECK (role IN ('creator','brand','admin','fan')),
            avatar_url      TEXT,
            bio             TEXT,
            country         TEXT,
            socials         TEXT,
            balance_cents   INTEGER NOT NULL DEFAULT 0,
            pending_cents   INTEGER NOT NULL DEFAULT 0,
            total_paid_cents INTEGER NOT NULL DEFAULT 0,
            payout_method   TEXT,
            payout_details  TEXT,
            lang            TEXT NOT NULL DEFAULT 'ar',
            banned          INTEGER NOT NULL DEFAULT 0,
            banned_reason   TEXT,
            banned_at       TEXT,
            brand_id        INTEGER REFERENCES users(id) ON DELETE CASCADE,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            cur.execute(new_users_sql)
            old_cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
            new_cols = [r[1] for r in cur.execute("PRAGMA table_info(users_new)").fetchall()]
            shared = [c for c in old_cols if c in new_cols]
            col_list = ",".join(shared)
            cur.execute(f"INSERT INTO users_new ({col_list}) SELECT {col_list} FROM users")
            cur.execute("DROP TABLE users")
            cur.execute("ALTER TABLE users_new RENAME TO users")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    # Fan email verification
    if "email_verified" not in cols:
        try:
            cur.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    cols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "email_verification_token" not in cols:
        try:
            cur.execute("ALTER TABLE users ADD COLUMN email_verification_token TEXT")
        except sqlite3.OperationalError:
            pass
    # Auto-verify existing brand/creator/admin (they were created by trusted parties)
    cur.execute("UPDATE users SET email_verified=1 WHERE role IN ('admin','brand','creator')")
    conn.commit()

    # view_clicks table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS view_clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
            fan_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            visitor_token TEXT,
            ip TEXT,
            ua TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_view_clicks_sub ON view_clicks(submission_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_view_clicks_fan ON view_clicks(fan_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_view_clicks_visitor ON view_clicks(submission_id, visitor_token)")

    conn.commit()
    conn.close()


def query(sql: str, params: tuple = ()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    return get_db().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


def execute_returning_rowcount(sql: str, params: tuple = ()) -> int:
    """Run an UPDATE and return the number of rows actually changed.
    Use for atomic conditional updates (race-safe balance decrement)."""
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.rowcount
