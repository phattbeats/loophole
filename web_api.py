"""
Web API for Loophole - exposes legal code, cases, and sessions via REST endpoints
"""
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import List, Optional
import os
from pathlib import Path
import sys

# Add the loophole package to path
sys.path.insert(0, str(Path(__file__).parent))

from loophole.main import app as typer_app
from loophole.session import SessionManager
from loophole.models import SessionState, LegalCode, CaseStatus, CaseType
from loophole.visualize import generate_html
import uvicorn

# Initialize FastAPI app
api = FastAPI(title="Loophole API", version="1.0.0")

# Security
security = HTTPBearer()

# Configuration
SESSION_DIR = os.getenv("LOOPHOLE_SESSION_DIR", "sessions")
DB_PATH = os.getenv("LOOPHOLE_DB_PATH", "sessions/loophole.db")

# Initialize session manager
session_manager = SessionManager(SESSION_DIR, DB_PATH)

# Pydantic models for API
class SessionInfo(BaseModel):
    id: str
    domain: str
    round: int
    cases: int
    code_version: int

class LegalCodeResponse(BaseModel):
    version: int
    text: str
    changelog: Optional[str] = None

class OutsideVoteResponse(BaseModel):
    voter_id: str
    vote: str  # "uphold" | "overturn" | "abstain"
    confidence: int
    voted_at: str


class CaseResponse(BaseModel):
    id: int
    scenario: str
    explanation: str
    case_type: str  # "LOOPHOLE" or "OVERREACH"
    round: int
    status: str
    resolution: Optional[str] = None
    resolved_by: Optional[str] = None
    outside_votes: list[OutsideVoteResponse] = []


class CaseVoteRequest(BaseModel):
    voter_id: str
    vote: str  # "uphold" | "overturn" | "abstain"
    confidence: int = Field(ge=1, le=5)


class CaseVoteResponse(BaseModel):
    ok: bool
    case_id: int
    outside_votes: list[OutsideVoteResponse]

class SessionDetailResponse(BaseModel):
    session_id: str
    domain: str
    moral_principles: str
    current_round: int
    cases: List[CaseResponse]
    legal_code: LegalCodeResponse
    code_history: List[LegalCodeResponse]

class SessionCreateRequest(BaseModel):
    domain: str
    principles_file: Optional[str] = None
    headless: bool = False
    rounds: int = 0

# Mock authentication (replace with real auth in production)
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    # In production, validate the token properly
    # For now, we'll accept any token for simplicity
    if credentials.credentials:
        return credentials.credentials
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

# API Endpoints
@api.get("/")
async def root():
    return {"message": "Loophole API is running"}

@api.get("/sessions", response_model=List[SessionInfo])
async def list_sessions(token: str = Depends(verify_token)):
    """List all sessions"""
    sessions = session_manager.list_sessions()
    return [
        SessionInfo(
            id=s["id"],
            domain=s["domain"],
            round=s["round"],
            cases=s["cases"],
            code_version=s["code_version"]
        )
        for s in sessions
    ]

@api.post("/sessions", response_model=SessionInfo)
async def create_session(request: SessionCreateRequest, token: str = Depends(verify_token)):
    """Create a new session"""
    # This would integrate with the typer app logic
    # For now, returning a placeholder
    return SessionInfo(
        id="placeholder",
        domain=request.domain,
        round=0,
        cases=0,
        code_version=1
    )

@api.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(session_id: str, token: str = Depends(verify_token)):
    """Get detailed session information"""
    try:
        state = session_manager.load(session_id)
        
        # Convert cases to API format
        cases = []
        for case in state.cases:
            cases.append(CaseResponse(
                id=case.id,
                scenario=case.scenario,
                explanation=case.explanation,
                case_type="LOOPHOLE" if case.case_type.value == "loophole" else "OVERREACH",
                round=case.round,
                status=case.status.value,
                resolution=case.resolution,
                resolved_by=case.resolved_by
            ))
        
        # Get current legal code
        current_code = state.current_code
        legal_code_response = LegalCodeResponse(
            version=current_code.version,
            text=current_code.text,
            changelog=current_code.changelog
        )
        
        # Get code history
        code_history = []
        for code in state.code_history:
            code_history.append(LegalCodeResponse(
                version=code.version,
                text=code.text,
                changelog=code.changelog
            ))
        
        return SessionDetailResponse(
            session_id=state.session_id,
            domain=state.domain,
            moral_principles=state.moral_principles,
            current_round=state.current_round,
            cases=cases,
            legal_code=legal_code_response,
            code_history=code_history
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Session not found: {str(e)}")

@api.get("/sessions/{session_id}/legal-code", response_model=LegalCodeResponse)
async def get_legal_code(session_id: str, token: str = Depends(verify_token)):
    """Get current legal code for a session"""
    try:
        state = session_manager.load(session_id)
        code = state.current_code
        return LegalCodeResponse(
            version=code.version,
            text=code.text,
            changelog=code.changelog
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Session not found: {str(e)}")

@api.post("/sessions/{session_id}/cases/{case_id}/vote", response_model=CaseVoteResponse)
async def submit_outside_vote(
    session_id: str,
    case_id: int,
    request: CaseVoteRequest,
    token: str = Depends(verify_token),
):
    """
    Submit an outside observer vote on a case.
    Outside votes are from non-agent humans or external agents.
    Vote: uphold | overturn | abstain. Confidence: 1-5.
    """
    try:
        state = session_manager.load(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Find the case
    case = next((c for c in state.cases if c.id == case_id), None)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found in session")

    # Validate vote value
    valid_votes = {"uphold", "overturn", "abstain"}
    if request.vote not in valid_votes:
        raise HTTPException(status_code=400, detail=f"Invalid vote: {request.vote}. Must be one of: {valid_votes}")

    # Record in SQLite if available
    store = session_manager._sqlite
    if store:
        store.record_outside_vote(case_id, request.voter_id, request.vote, request.confidence)

    # Also update in-memory state
    from loophole.models import OutsideVote, VoteValue
    from datetime import datetime
    new_vote = OutsideVote(
        voter_id=request.voter_id,
        vote=VoteValue(request.vote),
        confidence=request.confidence,
        voted_at=datetime.now(),
    )
    case.outside_votes.append(new_vote)
    session_manager.save(state)

    # Return updated votes
    votes = store.get_outside_votes(case_id) if store else []
    return CaseVoteResponse(
        ok=True,
        case_id=case_id,
        outside_votes=[
            OutsideVoteResponse(
                voter_id=v["voter_id"],
                vote=v["vote"],
                confidence=v["confidence"],
                voted_at=v["created_at"],
            )
            for v in votes
        ],
    )


@api.get("/sessions/{session_id}/cases/{case_id}", response_model=CaseResponse)
async def get_case(session_id: str, case_id: int, token: str = Depends(verify_token)):
    """Get a single case with its outside votes."""
    try:
        state = session_manager.load(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    case = next((c for c in state.cases if c.id == case_id), None)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found in session")

    # Get outside votes from SQLite
    store = session_manager._sqlite
    votes = []
    if store:
        rows = store.get_outside_votes(case_id)
        votes = [
            OutsideVoteResponse(
                voter_id=row["voter_id"],
                vote=row["vote"],
                confidence=row["confidence"],
                voted_at=row["created_at"],
            )
            for row in rows
        ]

    # Merge with in-memory votes
    for ov in case.outside_votes:
        if not any(v.voter_id == ov.voter_id and v.voted_at == ov.voted_at for v in votes):
            votes.append(OutsideVoteResponse(
                voter_id=ov.voter_id,
                vote=ov.vote.value,
                confidence=ov.confidence,
                voted_at=ov.voted_at.isoformat(),
            ))

    return CaseResponse(
        id=case.id,
        scenario=case.scenario,
        explanation=case.explanation,
        case_type="LOOPHOLE" if case.case_type.value == "loophole" else "OVERREACH",
        round=case.round,
        status=case.status.value,
        resolution=case.resolution,
        resolved_by=case.resolved_by,
        outside_votes=votes,
    )


@api.get("/sessions/{session_id}/cases", response_model=List[CaseResponse])
async def get_session_cases(session_id: str, token: str = Depends(verify_token)):
    """Get all cases for a session"""
    try:
        state = session_manager.load(session_id)
        cases = []
        store = session_manager._sqlite
        cases_out = []
        for case in state.cases:
            # Get outside votes from SQLite
            votes = []
            if store:
                rows = store.get_outside_votes(case.id)
                votes = [
                    OutsideVoteResponse(
                        voter_id=row["voter_id"],
                        vote=row["vote"],
                        confidence=row["confidence"],
                        voted_at=row["created_at"],
                    )
                    for row in rows
                ]
            # Merge with in-memory
            for ov in case.outside_votes:
                if not any(v.voter_id == ov.voter_id for v in votes):
                    votes.append(OutsideVoteResponse(
                        voter_id=ov.voter_id,
                        vote=ov.vote.value,
                        confidence=ov.confidence,
                        voted_at=ov.voted_at.isoformat(),
                    ))
            cases_out.append(CaseResponse(
                id=case.id,
                scenario=case.scenario,
                explanation=case.explanation,
                case_type="LOOPHOLE" if case.case_type.value == "loophole" else "OVERREACH",
                round=case.round,
                status=case.status.value,
                resolution=case.resolution,
                resolved_by=case.resolved_by,
                outside_votes=votes,
            ))
        return cases_out
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Session not found: {str(e)}")

@api.get("/sessions/{session_id}/visualize", response_class=HTMLResponse)
async def visualize_session(session_id: str, token: str = Depends(verify_token)):
    """Generate and return HTML visualization for a session"""
    try:
        state = session_manager.load(session_id)
        report_path = generate_html(state)
        with open(report_path, 'r') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Could not generate visualization: {str(e)}")

@api.get("/health")
async def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=8000)