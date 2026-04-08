"""Cost tracking for Loophole LLM calls.

Records every LLM call with timestamp, agent role, model, input/output tokens,
and computes cost using model-specific pricing.

Pricing is loaded from config (or env vars):
  LITELLM_PRICING  — JSON string mapping model name to {input_per_1k, output_per_1k}
  Default pricing for common models is built in.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Default pricing (per 1M tokens) — update as needed
# ---------------------------------------------------------------------------
DEFAULT_PRICING = {
    # GPT-4o family
    "gpt-4o": {"input_per_1k": 2.50, "output_per_1k": 10.00},
    "gpt-4o-mini": {"input_per_1k": 0.15, "output_per_1k": 0.60},
    # GPT-4 Turbo
    "gpt-4-turbo": {"input_per_1k": 10.00, "output_per_1k": 30.00},
    # Claude via LiteLLM
    "claude-3-5-sonnet-20241022": {"input_per_1k": 3.00, "output_per_1k": 15.00},
    "claude-3-5-sonnet": {"input_per_1k": 3.00, "output_per_1k": 15.00},
    "claude-3-opus": {"input_per_1k": 15.00, "output_per_1k": 75.00},
    "claude-3-sonnet": {"input_per_1k": 3.00, "output_per_1k": 15.00},
    "claude-3-haiku": {"input_per_1k": 0.25, "output_per_1k": 1.25},
    # Gemini
    "gemini-1.5-pro": {"input_per_1k": 1.25, "output_per_1k": 5.00},
    "gemini-1.5-flash": {"input_per_1k": 0.00, "output_per_1k": 0.00},
    # Fallback
    "unknown": {"input_per_1k": 0.00, "output_per_1k": 0.00},
}


def _load_pricing() -> dict:
    """Merge DEFAULT_PRICING with any user overrides from env var."""
    pricing = dict(DEFAULT_PRICING)
    raw = os.getenv("LITELLM_PRICING", "")
    if raw:
        try:
            overrides = json.loads(raw)
            pricing.update(overrides)
        except Exception as e:
            print(f"[COST] Invalid LITELLM_PRICING env var: {e}")
    return pricing


PRICING = _load_pricing()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CostRecord:
    timestamp: str        # ISO8601 UTC
    agent_role: str      # e.g. "legislator", "judge"
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    session_id: str

    def to_dict(self):
        return asdict(self)


class CostTracker:
    """
    In-memory + persistent JSON cost tracker.

    Records are cached in memory and flushed to a session-scoped JSON file
    on every write (append).  The global index (`global_index.json`) lists
    all session cost files for fast lookup.
    """

    def __init__(self, storage_dir: str = "sessions/costs"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._global_index_path = self.storage_dir / "global_index.json"
        self._session_records: list[CostRecord] = []
        self._session_file: Optional[Path] = None
        self._session_id: Optional[str] = None
        self._load_global_index()

    # ------------------------------------------------------------------
    # Session scoping
    # ------------------------------------------------------------------

    def start_session(self, session_id: str) -> None:
        self._session_id = session_id
        self._session_records = []
        self._session_file = self.storage_dir / f"{session_id}.json"
        if self._session_file.exists():
            try:
                self._session_records = [
                    CostRecord(**r) for r in json.loads(self._session_file.read_text())
                ]
            except Exception as e:
                print(f"[COST] Could not load session file {self._session_file}: {e}")
                self._session_records = []

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        agent_role: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        session_id: str,
    ) -> CostRecord:
        cost = compute_cost(model, input_tokens, output_tokens)
        record = CostRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_role=agent_role,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            session_id=session_id,
        )
        self._session_records.append(record)
        self._flush_session()
        self._ensure_session_in_index(session_id)
        return record

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def session_total(self, session_id: str) -> dict:
        """Return cost breakdown for a session."""
        records = self._load_session_records(session_id)
        by_agent: dict[str, dict] = {}
        total_input = total_output = total_cost = 0
        for r in records:
            total_input += r.input_tokens
            total_output += r.output_tokens
            total_cost += r.cost_usd
            by_agent.setdefault(r.agent_role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            ag = by_agent[r.agent_role]
            ag["calls"] += 1
            ag["input_tokens"] += r.input_tokens
            ag["output_tokens"] += r.output_tokens
            ag["cost_usd"] += r.cost_usd

        return {
            "session_id": session_id,
            "total_cost_usd": round(total_cost, 6),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_calls": len(records),
            "by_agent": {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in by_agent.items()},
        }

    def global_totals(self) -> dict:
        """Aggregate cost across all known sessions."""
        index = self._load_global_index()
        total_cost = total_input = total_output = total_calls = 0
        by_session: dict = {}
        for sid in index:
            records = self._load_session_records(sid)
            sess_cost = sum(r.cost_usd for r in records)
            sess_in = sum(r.input_tokens for r in records)
            sess_out = sum(r.output_tokens for r in records)
            total_cost += sess_cost
            total_input += sess_in
            total_output += sess_out
            total_calls += len(records)
            by_session[sid] = {
                "cost_usd": round(sess_cost, 6),
                "calls": len(records),
            }
        return {
            "global_cost_usd": round(total_cost, 6),
            "global_input_tokens": total_input,
            "global_output_tokens": total_output,
            "global_calls": total_calls,
            "sessions": by_session,
        }

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _flush_session(self) -> None:
        if self._session_file is None:
            return
        try:
            self._session_file.write_text(
                json.dumps([r.to_dict() for r in self._session_records], indent=2)
            )
        except Exception as e:
            print(f"[COST] Failed to flush session costs: {e}")

    def _ensure_session_in_index(self, session_id: str) -> None:
        index = self._load_global_index()
        if session_id not in index:
            index.append(session_id)
            self._global_index_path.write_text(json.dumps(index, indent=2))

    def _load_global_index(self) -> list[str]:
        if not self._global_index_path.exists():
            return []
        try:
            return json.loads(self._global_index_path.read_text())
        except Exception:
            return []

    def _load_session_records(self, session_id: str) -> list[CostRecord]:
        path = self.storage_dir / f"{session_id}.json"
        if not path.exists():
            return []
        try:
            return [CostRecord(**r) for r in json.loads(path.read_text())]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Report formatting
    # ------------------------------------------------------------------

    def report_session(self, session_id: str) -> str:
        data = self.session_total(session_id)
        lines = [
            f"=== Cost Report: {session_id} ===",
            f"Total cost : ${data['total_cost_usd']:.6f}",
            f"Input tokens: {data['total_input_tokens']:,}",
            f"Output tokens: {data['total_output_tokens']:,}",
            f"LLM calls: {data['total_calls']}",
            "",
            "By agent:",
        ]
        for agent, stats in data["by_agent"].items():
            lines.append(
                f"  {agent:<15} calls={stats['calls']:<5} "
                f"in={stats['input_tokens']:<10} out={stats['output_tokens']:<10} "
                f"cost=${stats['cost_usd']:.6f}"
            )
        return "\n".join(lines)

    def report_global(self) -> str:
        data = self.global_totals()
        lines = [
            "=== Global Cost Report ===",
            f"Global cost : ${data['global_cost_usd']:.6f}",
            f"Input tokens: {data['global_input_tokens']:,}",
            f"Output tokens: {data['global_output_tokens']:,}",
            f"LLM calls: {data['global_calls']}",
            "",
            "By session:",
        ]
        for sid, stats in data["sessions"].items():
            lines.append(f"  {sid:<30} calls={stats['calls']:<5} cost=${stats['cost_usd']:.6f}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------

def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute USD cost for a call using per-token pricing."""
    # Normalise model key: try exact, then prefix match
    pricing = PRICING.get(model, PRICING.get("unknown", {"input_per_1k": 0.0, "output_per_1k": 0.0}))
    input_cost = (input_tokens / 1000) * pricing["input_per_1k"]
    output_cost = (output_tokens / 1000) * pricing["output_per_1k"]
    return round(input_cost + output_cost, 7)


# ---------------------------------------------------------------------------
# Singleton instance for use inside LLMClient
# ---------------------------------------------------------------------------

_tracker: Optional[CostTracker] = None


def get_tracker() -> CostTracker:
    global _tracker
    if _tracker is None:
        _tracker = CostTracker()
    return _tracker
