"""SQLite store for proposals and reviewer decisions.

This is what makes daily re-runs safe:
  - A proposal's id is a hash of what it would change.
  - If that id already exists (pending, approved, rejected, or failed), the run skips it.
  - Pending proposals that the latest run no longer produces are marked 'stale',
    so a reviewer never approves a change based on outdated data.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "state.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    kind TEXT, category TEXT, title TEXT,
    target_account_id TEXT, website_slug TEXT,
    payload TEXT,
    status TEXT DEFAULT 'pending',     -- pending | approved | rejected | held | failed | stale
    first_seen TEXT, last_seen TEXT, decided_at TEXT,
    result TEXT
);
CREATE TABLE IF NOT EXISTS history (
    proposal_id TEXT, at TEXT, action TEXT, detail TEXT   -- audit trail of every decision
);
CREATE TABLE IF NOT EXISTS runs (
    run_at TEXT, locations INTEGER, accounts INTEGER,
    new_proposals INTEGER, skipped_decided INTEGER, marked_stale INTEGER, confirmed TEXT
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_run(proposals, confirmed, n_locations, n_accounts):
    conn = connect()
    ts = now()
    existing = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM proposals")}
    produced = {p["id"] for p in proposals}
    new = skipped = 0
    for p in proposals:
        if p["id"] in existing:
            skipped += existing[p["id"]] != "pending"
            if existing[p["id"]] == "pending":
                # Same change, but refresh the evidence so reviewers see the latest context
                conn.execute("UPDATE proposals SET last_seen=?, payload=? WHERE id=?", (ts, json.dumps(p), p["id"]))
            else:
                conn.execute("UPDATE proposals SET last_seen=? WHERE id=?", (ts, p["id"]))
            continue
        conn.execute(
            "INSERT INTO proposals (id, kind, category, title, target_account_id, website_slug, payload, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (p["id"], p["kind"], p["category"], p["title"], p["target_account_id"], p["website_slug"],
             json.dumps(p), ts, ts))
        new += 1
    stale = [pid for pid, st in existing.items() if st == "pending" and pid not in produced]
    for pid in stale:
        conn.execute("UPDATE proposals SET status='stale', result=? WHERE id=?",
                     (json.dumps({"reason": "No longer produced by the latest run; underlying data changed."}), pid))
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                 (ts, n_locations, n_accounts, new, skipped, len(stale), json.dumps(confirmed)))
    conn.commit()
    conn.close()
    return {"new": new, "skipped_decided": skipped, "stale": len(stale)}


def list_proposals(status=None):
    conn = connect()
    q = "SELECT * FROM proposals" + (" WHERE status=?" if status else "") + " ORDER BY category, title"
    rows = conn.execute(q, (status,) if status else ()).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        out.append(d)
    return out


def get_proposal(pid):
    rows = [p for p in list_proposals() if p["id"] == pid]
    return rows[0] if rows else None


def set_decision(pid, status, result=None):
    conn = connect()
    ts = now()
    conn.execute("UPDATE proposals SET status=?, decided_at=?, result=? WHERE id=?",
                 (status, ts, json.dumps(result) if result is not None else None, pid))
    conn.execute("INSERT INTO history VALUES (?,?,?,?)", (pid, ts, status, json.dumps(result)))
    conn.commit()
    conn.close()


REOPENABLE = ("rejected", "held", "failed")


def reopen(pid):
    """Send a rejected, held or failed proposal back to the pending queue. The audit trail is kept."""
    conn = connect()
    row = conn.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()
    if not row or row["status"] not in REOPENABLE:
        conn.close()
        raise ValueError(f"Only {', '.join(REOPENABLE)} proposals can be reopened")
    ts = now()
    conn.execute("UPDATE proposals SET status='pending', decided_at=NULL, result=NULL WHERE id=?", (pid,))
    conn.execute("INSERT INTO history VALUES (?,?,?,?)",
                 (pid, ts, "reopened", json.dumps({"from": row["status"]})))
    conn.commit()
    conn.close()


def history(pid):
    conn = connect()
    rows = conn.execute("SELECT at, action, detail FROM history WHERE proposal_id=? ORDER BY at", (pid,)).fetchall()
    conn.close()
    return [{"at": r["at"], "action": r["action"], "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]


def last_run():
    conn = connect()
    r = conn.execute("SELECT * FROM runs ORDER BY run_at DESC LIMIT 1").fetchone()
    conn.close()
    if not r:
        return None
    d = dict(r)
    d["confirmed"] = json.loads(d["confirmed"])
    return d
