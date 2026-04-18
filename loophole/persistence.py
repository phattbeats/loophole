"""
persistence.py — SQLite backend for Loophole session storage.

Adds:
  - Persistent cases table (full-text searchable)
  - Votes table (per-agent votes with confidence)
  - Sessions table (per-round agent responses for audit trail)
  - Case precedent lookup (find similar past cases by scenario fingerprint)
  - Audit trail for compliance

Swappable with the JSON file backend: pass `backend="sqlite"` or `backend="json"`
to SessionManager to choose.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from loophole.models import (
    Case,
    CaseStatus,
    CaseType,
    LegalCode,
    SessionState,
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT    NOT NULL,
    round           INTEGER NOT NULL,
    case_type       TEXT    NOT NULL,   -- 'loophole' | 'overreach'
    scenario        TEXT    NOT NULL,
    explanation     TEXT    NOT NULL,
    status          TEXT    NOT NULL,   -- 'pending' | 'auto_resolved' | 'escalated' | 'user_resolved'
    resolution      TEXT,
    resolved_by     TEXT,              -- 'judge' | 'user' | 'judge-auto'
    created_at     TEXT    NOT NULL   -- ISO-8601
);

CREATE TABLE IF NOT EXISTS outside_votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL REFERENCES cases(id),
    voter_id    TEXT    NOT NULL,
    vote        TEXT    NOT NULL,   -- 'uphold' | 'overturn' | 'abstain'
    confidence  INTEGER NOT NULL,   -- 1-5
    created_at  TEXT    NOT NULL   -- ISO-8601
);

CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL REFERENCES cases(id),
    voter_id    TEXT    NOT NULL,
    role        TEXT    NOT NULL,   -- 'judge' | 'loophole_finder' | 'overreach_finder' | 'legislator'
    vote        TEXT    NOT NULL,   -- 'resolve' | 'escalate' | 'reject'
    confidence  REAL,
    reasoning  TEXT,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    round       INTEGER NOT NULL,
    agent_id    TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    response    TEXT,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS case_summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

-- Indexes for common lookups
CREATE INDEX IF NOT EXISTS idx_cases_session   ON cases(session_id);
CREATE INDEX IF NOT EXISTS idx_cases_status     ON cases(status);
CREATE INDEX IF NOT EXISTS idx_cases_type        ON cases(case_type);
CREATE INDEX IF NOT EXISTS idx_cases_created    ON cases(created_at);
CREATE INDEX IF NOT EXISTS idx_votes_case       ON votes(case_id);
CREATE INDEX IF NOT EXISTS idx_outside_votes_case ON outside_votes(case_id);
CREATE INDEX IF NOT EXISTS idx_agent_sess       ON agent_sessions(session_id, round);
"""

# ---------------------------------------------------------------------------
# JSON serialization helpers
# ---------------------------------------------------------------------------

def _dt_to_str(dt: datetime) -> str:
    return dt.isoformat()


def _str_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _case_from_row(row: tuple) -> Case:
    return Case(
        id=int(row[0]),
        session_id=row[1],
        round=int(row[2]),
        case_type=CaseType(row[3]),
        scenario=row[4],
        explanation=row[5],
        status=CaseStatus(row[6]),
        resolution=row[7],
        resolved_by=row[8],
        created_at=_str_to_dt(row[9]),
    )


def _case_to_row(c: Case) -> tuple:
    return (
        c.id,
        c.session_id,
        c.round,
        c.case_type.value,
        c.scenario,
        c.explanation,
        c.status.value,
        c.resolution,
        c.resolved_by,
        _dt_to_str(c.created_at),
    )


# ---------------------------------------------------------------------------
# SQLiteStore
# ---------------------------------------------------------------------------

class SQLiteStore:
    """
    SQLite-backed session store. Provides full-text search across all
    resolved cases and a complete audit trail of all agent responses.

    Usage:
        store = SQLiteStore("sessions/loophole.db")
        store.init()

        # Save a case
        store.save_case(session_id, case)

        # Record an agent response
        store.record_response(session_id, round, agent_id, role, response_text)

        # Precedent lookup
        similar = store.find_similar_cases(scenario_text, limit=5)

        # Full audit
        audit = store.get_session_audit(session_id)
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def init(self) -> None:
        """Create tables if they don't exist. Call once on startup."""
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Thread-safe connection context manager."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---- Cases ------------------------------------------------------------

    def save_case(self, state: SessionState, case: Case) -> None:
        """Insert or update a case. Upsert on id."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO cases (id, session_id, round, case_type, scenario, explanation,
                                  status, resolution, resolved_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    resolution=excluded.resolution,
                    resolved_by=excluded.resolved_by
            """, _case_to_row(case))
            conn.commit()

    def save_cases_batch(self, state: SessionState) -> None:
        """Save all cases from a SessionState in one transaction."""
        with self._conn() as conn:
            for case in state.cases:
                conn.execute("""
                    INSERT INTO cases (id, session_id, round, case_type, scenario, explanation,
                                      status, resolution, resolved_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        resolution=excluded.resolution,
                        resolved_by=excluded.resolved_by
                """, _case_to_row(case))
            conn.commit()

    def load_cases(self, session_id: str) -> list[Case]:
        """Load all cases for a session, ordered by creation time."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cases WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            return [_case_from_row(tuple(row)) for row in rows]

    def get_case(self, case_id: int) -> Optional[Case]:
        """Get a single case by ID."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE id=?", (case_id,),
            ).fetchone()
            return _case_from_row(tuple(row)) if row else None

    def find_similar_cases(
        self,
        scenario_text: str,
        session_id: Optional[str] = None,
        limit: int = 5,
    ) -> list[Case]:
        """
        Find cases with similar scenarios using LIKE substring match.
        For production, replace with FTS5 for better relevance ranking.

        Pass session_id to restrict search to a specific session.
        """
        pattern = f"%{scenario_text[:100]}%"
        with self._conn() as conn:
            if session_id:
                rows = conn.execute("""
                    SELECT * FROM cases
                    WHERE session_id=? AND status IN ('auto_resolved', 'user_resolved')
                      AND scenario LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (session_id, pattern, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM cases
                    WHERE status IN ('auto_resolved', 'user_resolved')
                      AND scenario LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (pattern, limit)).fetchall()
            return [_case_from_row(tuple(row)) for row in rows]

    def get_case_count(self, session_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM cases WHERE session_id=?",
                (session_id,),
            ).fetchone()
            return row[0] if row else 0

    # ---- Votes -------------------------------------------------------------

    def record_vote(
        self,
        case_id: int,
        voter_id: str,
        role: str,
        vote: str,
        confidence: Optional[float] = None,
        reasoning: Optional[str] = None,
    ) -> None:
        """Record an agent's vote on a case."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO votes (case_id, voter_id, role, vote, confidence, reasoning, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (case_id, voter_id, role, vote, confidence, reasoning, _dt_to_str(datetime.now())))
            conn.commit()

    def get_votes(self, case_id: int) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM votes WHERE case_id=?", (case_id,),
            ).fetchall()

    # ---- Outside Votes -----------------------------------------------------

    def record_outside_vote(
        self,
        case_id: int,
        voter_id: str,
        vote: str,
        confidence: int,
    ) -> None:
        """Record an outside observer's vote on a case."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO outside_votes (case_id, voter_id, vote, confidence, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (case_id, voter_id, vote, confidence, _dt_to_str(datetime.now())))
            conn.commit()

    def get_outside_votes(self, case_id: int) -> list[dict]:
        """Get all outside votes for a case."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT voter_id, vote, confidence, created_at FROM outside_votes WHERE case_id=? ORDER BY created_at",
                (case_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    # ---- Agent sessions (audit log) ----------------------------------------

    def record_response(
        self,
        session_id: str,
        round_num: int,
        agent_id: str,
        role: str,
        response: str,
    ) -> None:
        """
        Record every agent's output per round. This is the full audit trail.
        Used for compliance reporting and precedent lookup.
        """
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO agent_sessions (session_id, round, agent_id, role, response, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, round_num, agent_id, role, response, _dt_to_str(datetime.now())))
            conn.commit()

    def get_session_audit(self, session_id: str) -> dict:
        """Return full audit trail for a session."""
        with self._conn() as conn:
            cases = conn.execute(
                "SELECT * FROM cases WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            responses = conn.execute(
                "SELECT * FROM agent_sessions WHERE session_id=? ORDER BY round, agent_id",
                (session_id,),
            ).fetchall()
            summaries = conn.execute(
                "SELECT * FROM case_summaries WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return {
            "session_id": session_id,
            "cases": [dict(row) for row in cases],
            "responses": [dict(row) for row in responses],
            "case_summaries": [dict(row) for row in summaries],
        }

    def record_case_summary(self, session_id: str, summary: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO case_summaries (session_id, summary, created_at) VALUES (?, ?, ?)",
                (session_id, summary, _dt_to_str(datetime.now())),
            )
            conn.commit()

    def load_case_summaries(self, session_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT summary FROM case_summaries WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            return [row[0] for row in rows]

    # ---- Session metadata -------------------------------------------------

    def get_session_metadata(self, session_id: str) -> Optional[dict]:
        """Return a summary row for session listing."""
        with self._conn() as conn:
            row = conn.execute("""
                SELECT session_id,
                       MIN(created_at) as started_at,
                       MAX(created_at) as last_activity,
                       COUNT(DISTINCT id) as case_count,
                       SUM(CASE WHEN status IN ('auto_resolved','user_resolved') THEN 1 ELSE 0 END) as resolved_count
                FROM cases WHERE session_id=?
                GROUP BY session_id
            """, (session_id,)).fetchone()
            return dict(row) if row else None

    def list_sessions(self) -> list[dict]:
        """List all sessions with case counts."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT session_id,
                       MIN(created_at) as started_at,
                       MAX(created_at) as last_activity,
                       COUNT(DISTINCT id) as case_count,
                       SUM(CASE WHEN status IN ('auto_resolved','user_resolved') THEN 1 ELSE 0 END) as resolved_count
                FROM cases
                GROUP BY session_id
                ORDER BY last_activity DESC
            """).fetchall()
            return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# JSON file store (existing behavior, kept for backward compat)
# ---------------------------------------------------------------------------

class JSONFileStore:
    """
    Wraps the existing JSON-file SessionManager behavior as a drop-in store.
    Used when backend="json" or when SQLite is not yet initialized.
    """

    def __init__(self, base_dir: str = "sessions"):
        from loophole.session import SessionManager as FileSessionManager
        self._mgr = FileSessionManager(base_dir)

    def save_cases_batch(self, state: SessionState) -> None:
        self._mgr.save(state)

    def load_cases(self, session_id: str) -> list[Case]:
        return self._mgr.load(session_id).cases

    def load_case_summaries(self, session_id: str) -> list[str]:
        return self._mgr.load(session_id).case_summaries

    def find_similar_cases(self, scenario_text: str, session_id: Optional[str] = None, limit: int = 5) -> list[Case]:
        # JSON file store doesn't support cross-session search
        return []

    def get_session_audit(self, session_id: str) -> dict:
        return {"note": "Audit trail requires SQLite backend"}

    def list_sessions(self) -> list[dict]:
        sessions = self._mgr.list_sessions()
        return [{"session_id": s["id"], "case_count": s["cases"]} for s in sessions]
