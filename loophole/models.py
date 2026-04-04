from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class CaseType(str, Enum):
    LOOPHOLE = "loophole"  # Legal but immoral
    OVERREACH = "overreach"  # Illegal but moral


class CaseStatus(str, Enum):
    PENDING = "pending"
    AUTO_RESOLVED = "auto_resolved"
    ESCALATED = "escalated"
    USER_RESOLVED = "user_resolved"


class Case(BaseModel):
    id: int
    round: int
    case_type: CaseType
    scenario: str
    explanation: str
    status: CaseStatus = CaseStatus.PENDING
    resolution: str | None = None
    resolved_by: str | None = None  # "judge" or "user"
    created_at: datetime = Field(default_factory=datetime.now)


class LegalCode(BaseModel):
    version: int
    text: str
    changelog: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class SessionState(BaseModel):
    session_id: str
    domain: str
    moral_principles: str
    user_clarifications: list[str] = Field(default_factory=list)
    current_code: LegalCode
    code_history: list[LegalCode] = Field(default_factory=list)
    cases: list[Case] = Field(default_factory=list)
    current_round: int = 0
    created_at: datetime = Field(default_factory=datetime.now)

    @property
    def resolved_cases(self) -> list[Case]:
        return [
            c
            for c in self.cases
            if c.status in (CaseStatus.AUTO_RESOLVED, CaseStatus.USER_RESOLVED)
        ]

    @property
    def next_case_id(self) -> int:
        return len(self.cases) + 1
