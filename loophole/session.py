from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from loophole.models import Case, CaseStatus, LegalCode, SessionState
from loophole.persistence import SQLiteStore

# --- Token estimation and context window management ---

def estimate_tokens(text: str) -> int:
    """Rough token count: characters divided by 4 (conservative)."""
    return (len(text) + 3) // 4

def compute_context_tokens(state: SessionState) -> int:
    total = 0
    total += estimate_tokens(state.moral_principles)
    total += estimate_tokens(state.current_code.text)
    total += estimate_tokens("\n".join(state.user_clarifications))
    for c in state.cases:
        total += estimate_tokens(c.scenario)
        total += estimate_tokens(c.explanation)
        if c.resolution:
            total += estimate_tokens(c.resolution)
    total += estimate_tokens("\n".join(state.case_summaries))
    return total

def summarize_case(llm_client, case: Case) -> str:
    """Generate a one-sentence summary for a resolved case."""
    prompt = f"""Summarize this resolved case in one concise sentence that captures the key constraint or precedent it established.

Case #{case.id} ({case.case_type.value})
Scenario: {case.scenario}
Explanation: {case.explanation}
Resolution: {case.resolution}
Resolved by: {case.resolved_by}

One-sentence summary:"""
    summary = llm_client.call(
        system="You are a legal summarist. Produce a single, clear sentence that captures the essential precedent from this case.",
        user_message=prompt,
        temperature=0.1,
    )
    summary = summary.strip().strip('\'"\n')
    return f"Case #{case.id} ({case.case_type.value}): {summary}"

def enforce_context_window(state: SessionState, llm_client, max_tokens: int):
    """Summarize and prune oldest resolved cases if token count exceeds max_tokens."""
    MIN_RECENT = 3
    total = compute_context_tokens(state)
    if total <= max_tokens:
        return
    resolved = [c for c in state.cases if c.status in (CaseStatus.AUTO_RESOLVED, CaseStatus.USER_RESOLVED)]
    resolved.sort(key=lambda c: c.created_at)
    candidates = resolved[:-MIN_RECENT] if len(resolved) > MIN_RECENT else []
    if not candidates:
        return
    for case in candidates:
        try:
            summary = summarize_case(llm_client, case)
            state.case_summaries.append(summary)
            state.cases.remove(case)
        except Exception as e:
            print(f"[WARN] Failed to summarize case #{case.id}: {e}")
            continue
        total = compute_context_tokens(state)
        if total <= max_tokens:
            break


# --- SessionManager with optional SQLite backend ---

class SessionManager:
    """
    Manages session state with optional SQLite backend for persistent storage,
    full-text search, and audit trail.

    Set backend="sqlite" to enable SQLite (default when db_path is set).
    Set backend="json" for the existing JSON file behavior.
    """

    def __init__(self, base_dir: str = "sessions", db_path: Optional[str] = None):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        if db_path:
            self._sqlite: Optional[SQLiteStore] = SQLiteStore(db_path)
            self._sqlite.init()
        else:
            self._sqlite = None

    @property
    def sqlite(self) -> Optional[SQLiteStore]:
        """SQLite store, if initialized. None if using JSON backend."""
        return self._sqlite

    def create_session(
        self,
        session_id: str,
        domain: str,
        principles: str,
        initial_code: LegalCode,
    ) -> SessionState:
        state = SessionState(
            session_id=session_id,
            domain=domain,
            moral_principles=principles,
            current_code=initial_code,
            code_history=[initial_code],
        )
        self.save(state)
        return state

    def save(self, state: SessionState) -> None:
        # Always persist JSON for backward compat and human readability
        session_dir = self.base_dir / state.session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        (session_dir / "state.json").write_text(state.model_dump_json(indent=2))
        (session_dir / "current_code.md").write_text(
            f"# Legal Code v{state.current_code.version}\n\n"
            f"*Domain: {state.domain}*\n\n{state.current_code.text}\n"
        )
        (session_dir / "case_log.md").write_text(_render_case_log(state))

        # SQLite: upsert all cases + summaries
        if self._sqlite:
            self._sqlite.save_cases_batch(state)
            for summary in state.case_summaries:
                # Only record if not already present (avoid dupes after prune+reload)
                existing = self._sqlite.load_case_summaries(state.session_id)
                if summary not in existing:
                    self._sqlite.record_case_summary(state.session_id, summary)

    def load(self, session_id: str) -> SessionState:
        state_path = self.base_dir / session_id / "state.json"
        try:
            return SessionState.model_validate_json(state_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            raise FileNotFoundError(f"Session {session_id} has corrupted state.json: {e}") from e

    def list_sessions(self) -> list[dict]:
        sessions = []
        try:
            entries = sorted(self.base_dir.iterdir())
        except OSError as e:
            # Broken symlink or permission issue in sessions dir — partial list only
            return []
        for p in entries:
            try:
                if not p.is_dir():
                    continue
                state_path = p / "state.json"
                # exists() returns False for broken symlinks; read_text() catches the rest
                if state_path.exists():
                    data = json.loads(state_path.read_text())
                    sessions.append({
                        "id": data["session_id"],
                        "domain": data["domain"],
                        "round": data["current_round"],
                        "cases": len(data["cases"]),
                        "code_version": data["current_code"]["version"],
                    })
            except (json.JSONDecodeError, OSError, KeyError):
                # Malformed directory or corrupted state.json — skip, don't crash the list
                continue
        return sessions

    # ---- SQLite-only queries (no-op on JSON backend) ----

    def record_response(
        self,
        session_id: str,
        round_num: int,
        agent_id: str,
        role: str,
        response: str,
    ) -> None:
        """Record agent response for audit trail. Requires SQLite backend."""
        if self._sqlite:
            self._sqlite.record_response(session_id, round_num, agent_id, role, response)

    def find_similar_cases(
        self,
        scenario_text: str,
        session_id: Optional[str] = None,
        limit: int = 5,
    ) -> list[Case]:
        """Full-text search across resolved cases. Requires SQLite backend."""
        if self._sqlite:
            return self._sqlite.find_similar_cases(scenario_text, session_id, limit)
        return []

    def record_vote(
        self,
        case_id: int,
        voter_id: str,
        role: str,
        vote: str,
        confidence: Optional[float] = None,
        reasoning: Optional[str] = None,
    ) -> None:
        """Record an agent vote. Requires SQLite backend."""
        if self._sqlite:
            self._sqlite.record_vote(case_id, voter_id, role, vote, confidence, reasoning)

    def get_session_audit(self, session_id: str) -> Optional[dict]:
        """Full audit trail. Requires SQLite backend."""
        if self._sqlite:
            return self._sqlite.get_session_audit(session_id)
        return None


def _render_case_log(state: SessionState) -> str:
    lines = [f"# Case Log — {state.domain}", f"*Session: {state.session_id}*\n"]

    status_labels = {
        CaseStatus.AUTO_RESOLVED: "Auto-resolved by Judge",
        CaseStatus.USER_RESOLVED: "Resolved by User",
        CaseStatus.ESCALATED: "ESCALATED — awaiting user",
        CaseStatus.PENDING: "Pending",
    }

    for case in state.cases:
        label = status_labels.get(case.status, case.status.value)
        lines.append(f"## Case #{case.id} ({case.case_type.value}) — Round {case.round}")
        lines.append(f"**Status:** {label}\n")
        lines.append(f"**Scenario:** {case.scenario}\n")
        lines.append(f"**Problem:** {case.explanation}\n")
        if case.resolution:
            lines.append(f"**Resolution:** {case.resolution}\n")
        lines.append("---\n")

    return "\n".join(lines)