from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loophole.models import Case, CaseStatus, LegalCode, SessionState

# --- Token estimation and context window management ---

def estimate_tokens(text: str) -> int:
    """Rough token count: characters divided by 4 (conservative)."""
    return (len(text) + 3) // 4

def compute_context_tokens(state: SessionState) -> int:
    total = 0
    total += estimate_tokens(state.moral_principles)
    total += estimate_tokens(state.current_code.text)
    total += estimate_tokens("\n".join(state.user_clarifications))
    # Cases: scenario + explanation + resolution
    for c in state.cases:
        total += estimate_tokens(c.scenario)
        total += estimate_tokens(c.explanation)
        if c.resolution:
            total += estimate_tokens(c.resolution)
    # Summaries
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

# --- End context management ---

class SessionManager:
    def __init__(self, base_dir: str = "sessions"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_session(self, session_id: str, domain: str, principles: str, initial_code: LegalCode) -> SessionState:
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
        session_dir = self.base_dir / state.session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Machine-readable state
        (session_dir / "state.json").write_text(
            state.model_dump_json(indent=2)
        )

        # Human-readable legal code
        (session_dir / "current_code.md").write_text(
            f"# Legal Code v{state.current_code.version}\n\n"
            f"*Domain: {state.domain}*\n\n"
            f"{state.current_code.text}\n"
        )

        # Human-readable case log
        (session_dir / "case_log.md").write_text(
            _render_case_log(state)
        )

    def load(self, session_id: str) -> SessionState:
        state_path = self.base_dir / session_id / "state.json"
        return SessionState.model_validate_json(state_path.read_text())

    def list_sessions(self) -> list[dict]:
        sessions = []
        for p in sorted(self.base_dir.iterdir()):
            state_path = p / "state.json"
            if state_path.exists():
                data = json.loads(state_path.read_text())
                sessions.append({
                    "id": data["session_id"],
                    "domain": data["domain"],
                    "round": data["current_round"],
                    "cases": len(data["cases"]),
                    "code_version": data["current_code"]["version"],
                })
        return sessions


def _render_case_log(state: SessionState) -> str:
    lines = [
        f"# Case Log — {state.domain}",
        f"*Session: {state.session_id}*\n",
    ]

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
