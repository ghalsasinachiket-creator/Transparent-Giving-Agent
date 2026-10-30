"""ledger.py — SQLite storage: donor rules, every decision, every order."""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone

from rules_engine import DonorRules, Decision

# DB file lives next to this script, so it works from any working directory.
DB = os.path.join(os.path.dirname(__file__), "giving.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row          # lets us read columns by name
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    """Create tables if they don't exist yet. Safe to call on every startup."""
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS rules (        -- always exactly one row (id=1)
            id INTEGER PRIMARY KEY CHECK (id = 1),
            json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decisions (    -- one row per evaluation, incl. blocked
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ein TEXT NOT NULL,
            charity_name TEXT NOT NULL,
            amount REAL NOT NULL,
            verdict TEXT NOT NULL,                -- APPROVE / FLAG / BLOCK
            results_json TEXT NOT NULL            -- the full checklist + evidence links
        );
        CREATE TABLE IF NOT EXISTS orders (       -- one row per PayPal order we created
            order_id TEXT PRIMARY KEY,
            decision_id INTEGER NOT NULL REFERENCES decisions(id),
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,                 -- CREATED -> COMPLETED
            capture_id TEXT
        );
        """)


# ---------- donor rules ----------
def get_rules() -> DonorRules:
    with _conn() as c:
        row = c.execute("SELECT json FROM rules WHERE id = 1").fetchone()
    # No saved rules yet -> fall back to the defaults in DonorRules
    return DonorRules(**json.loads(row["json"])) if row else DonorRules()


def save_rules(rules: DonorRules) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO rules (id, json) VALUES (1, ?)",
                  (json.dumps(rules.__dict__),))


# ---------- decisions ----------
def record_decision(ein: str, name: str, amount: float, d: Decision) -> int:
    """Store a decision (approved OR blocked) and return its id."""
    results = [{"rule": r.rule, "verdict": r.verdict.name,
                "detail": r.detail, "evidence_url": r.evidence_url}
               for r in d.results]
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO decisions (created_at, ein, charity_name, amount, verdict, results_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), ein, name, amount, d.verdict.name, json.dumps(results)))
        return cur.lastrowid


def get_decision(decision_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["results"] = json.loads(out.pop("results_json"))
    return out


# ---------- orders ----------
def record_order(order_id: str, decision_id: int) -> None:
    with _conn() as c:
        c.execute("INSERT INTO orders (order_id, decision_id, created_at, status)"
                  " VALUES (?, ?, ?, 'CREATED')", (order_id, decision_id, _now()))


def mark_completed(order_id: str, capture_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE orders SET status = 'COMPLETED', capture_id = ? WHERE order_id = ?",
                  (capture_id, order_id))


def month_total() -> float:
    """Sum of COMPLETED donations this calendar month (feeds the monthly-cap rule)."""
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")   # e.g. "2026-10"
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(d.amount), 0) AS total FROM orders o"
            " JOIN decisions d ON d.id = o.decision_id"
            " WHERE o.status = 'COMPLETED' AND substr(o.created_at, 1, 7) = ?",
            (prefix,)).fetchone()
    return float(row["total"])


def list_ledger(limit: int = 50) -> list[dict]:
    """Newest decisions first, each with its order (if one was created)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT d.*, o.order_id, o.status AS order_status, o.capture_id"
            " FROM decisions d LEFT JOIN orders o ON o.decision_id = d.id"
            " ORDER BY d.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["results"] = json.loads(item.pop("results_json"))
        out.append(item)
    return out