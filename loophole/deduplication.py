"""Deduplication module for Loophole sessions.

Prevents processing the same scenario multiple times by fingerprinting
each case (scenario + moral principles) and maintaining a persistent
index of seen cases across runs.

Storage format: JSON file mapping fingerprint -> {
    "session_id": str,
    "case_id": int,
    "resolution": str | None,
    "resolved_by": str | None,
    "timestamp": ISO8601 string
}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class DedupIndexEntry:
    session_id: str
    case_id: int
    resolution: Optional[str] = None
    resolved_by: Optional[str] = None
    timestamp: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DedupIndexEntry":
        return cls(**data)


class DeduplicationStore:
    """Simple JSON-based persistent store for case fingerprints."""

    def __init__(self, storage_path: str = "sessions/dedup_index.json"):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, DedupIndexEntry] = {}
        self._load()

    def _load(self) -> None:
        if self.storage_path.exists():
            try:
                raw = json.loads(self.storage_path.read_text())
                for fp, entry_dict in raw.items():
                    self._index[fp] = DedupIndexEntry.from_dict(entry_dict)
            except Exception as e:
                print(f"[DEDUP] Failed to load index: {e}")
                self._index = {}

    def _save(self) -> None:
        try:
            serializable = {fp: entry.to_dict() for fp, entry in self._index.items()}
            self.storage_path.write_text(json.dumps(serializable, indent=2))
        except Exception as e:
            print(f"[DEDUP] Failed to save index: {e}")

    def fingerprint(self, scenario: str, moral_principles: str) -> str:
        """Compute a SHA256 fingerprint for a case.

        Combines scenario and moral principles to differentiate across contexts.
        Normalizes whitespace to avoid trivial differences.
        """
        combined = f"{moral_principles.strip()}\n---\n{scenario.strip()}"
        # Normalize: collapse multiple spaces, strip line-end whitespace
        combined = " ".join(combined.split())
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def is_duplicate(self, fingerprint: str) -> bool:
        return fingerprint in self._index

    def get_entry(self, fingerprint: str) -> Optional[DedupIndexEntry]:
        return self._index.get(fingerprint)

    def record(self, fingerprint: str, session_id: str, case_id: int,
               resolution: Optional[str] = None, resolved_by: Optional[str] = None) -> None:
        """Record a case in the index."""
        entry = DedupIndexEntry(
            session_id=session_id,
            case_id=case_id,
            resolution=resolution,
            resolved_by=resolved_by,
            timestamp=datetime.utcnow().isoformat() + "Z",
        )
        self._index[fingerprint] = entry
        self._save()

    def prune_old(self, keep_last_n: int = 1000) -> None:
        """If index grows too large, drop older entries keeping the most recent N."""
        if len(self._index) <= keep_last_n:
            return
        sorted_entries = sorted(
            self._index.items(),
            key=lambda kv: kv[1].timestamp or "",
            reverse=True
        )
        self._index = dict(sorted_entries[:keep_last_n])
        self._save()
        print(f"[DEDUP] Pruned index to {len(self._index)} entries")
