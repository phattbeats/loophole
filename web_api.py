"""
Web API for Loophole - exposes legal code, cases, and sessions via REST endpoints
"""
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import List, Optional
import os
import hashlib
import time
from pathlib import Path
import sys

# Add the loophole package to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, '/root/.openclaw/utilities/python-packages')

from loophole.main import app as typer_app
from loophole.session import SessionManager
from loophole.models import SessionState, LegalCode, CaseStatus, CaseType, RoundType
from loophole.visualize import generate_html
from fastapi.staticfiles import StaticFiles
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

class OutsideVotesSummary(BaseModel):
    upholds: int = 0
    overturns: int = 0
    abstains: int = 0
    total: int = 0

class PIIStats(BaseModel):
    redactions_applied: int = 0
    categories: List[str] = []

class ContextWindowUsage(BaseModel):
    current_tokens: int = 0
    max_tokens: int = 60000

class SessionDetailResponse(BaseModel):
    session_id: str
    domain: str
    moral_principles: str
    current_round: int
    cases: List[CaseResponse]
    legal_code: LegalCodeResponse
    code_history: List[LegalCodeResponse]
    outside_votes_summary: OutsideVotesSummary
    pii_stats: PIIStats
    context_window_usage: ContextWindowUsage

class SessionCreateRequest(BaseModel):
    domain: str
    moral_principles: str
    user_clarifications: Optional[str] = None
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
    """Create a new Loophole session."""
    from loophole.models import LegalCode
    from loophole.agents.legislator import LegislatorAgent
    from loophole.llm import LLMClient

    import hashlib, time
    # Generate a short deterministic session ID from domain + random suffix
    raw = f"{request.domain}:{time.time_ns()}:{request.moral_principles[:50]}"
    session_id = hashlib.sha256(raw.encode()).hexdigest()[:16]
    config = session_manager._load_config() if hasattr(session_manager, '_load_config') else {}

    # Draft initial legal code via legislator
    try:
        llm_client = LLMClient(model=config.get("model", "claude-3-5-sonnet-20241022"))
        legislator = LegislatorAgent(llm_client, request.domain, request.moral_principles)
        placeholder = f"Initial legal code for {request.domain}. Principles: {request.moral_principles[:200]}"
        initial_code = legislator.draft_initial(placeholder)
    except Exception as e:
        initial_code = LegalCode(version=0, text="", changelog="Created via API")

    state = session_manager.create_session(
        session_id=session_id,
        domain=request.domain,
        principles=request.moral_principles,
        initial_code=initial_code,
    )
    return SessionInfo(
        id=state.session_id,
        domain=state.domain,
        round=state.current_round,
        cases=len(state.cases),
        code_version=state.current_code.version,
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
        
        # Compute outside_votes_summary across all resolved cases
        from collections import Counter
        vote_counts = Counter()
        total_votes = 0
        for case in state.cases:
            for ov in case.outside_votes:
                # ov is an OutsideVote model — ov.vote is a VoteValue enum
                vote_counts[ov.vote.value] += 1
                total_votes += 1

        # PII stats placeholder — PII redaction tracked separately per PHA-190
        pii_stats = PIIStats(redactions_applied=0, categories=[])

        # Context window usage (estimate from code + case text)
        code_tokens = len(state.current_code.text) // 4
        case_tokens = sum(len(c.scenario) // 4 for c in state.cases)
        current_tokens = code_tokens + case_tokens

        return SessionDetailResponse(
            session_id=state.session_id,
            domain=state.domain,
            moral_principles=state.moral_principles,
            current_round=state.current_round,
            cases=cases,
            legal_code=legal_code_response,
            code_history=code_history,
            outside_votes_summary=OutsideVotesSummary(
                upholds=vote_counts.get('uphold', 0),
                overturns=vote_counts.get('overturn', 0),
                abstains=vote_counts.get('abstain', 0),
                total=total_votes,
            ),
            pii_stats=pii_stats,
            context_window_usage=ContextWindowUsage(
                current_tokens=current_tokens,
                max_tokens=60000,
            ),
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


@api.get("/sessions/{session_id}/code-history", response_model=List[LegalCodeResponse])
async def get_code_history(session_id: str, token: str = Depends(verify_token)):
    """Get full version history of legal code with diffs between versions."""
    try:
        state = session_manager.load(session_id)
        history = []
        for code in state.code_history:
            history.append(LegalCodeResponse(
                version=code.version,
                text=code.text,
                changelog=code.changelog,
            ))
        return history
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Session not found: {str(e)}")

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

# Mount static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    api.mount("/static", StaticFiles(directory=str(static_dir), html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=8000)
# ─── Cost Tracking Endpoints ──────────────────────────────────────────────────

@api.get("/costs", response_model=dict)
async def get_global_costs(token: str = Depends(verify_token)):
    """Global cost totals across all sessions."""
    from loophole.cost_tracker import get_tracker
    tracker = get_tracker()
    return tracker.global_totals()

@api.get("/costs/sessions")
async def list_session_costs(token: str = Depends(verify_token)):
    """List all sessions with cost data."""
    from loophole.cost_tracker import get_tracker
    tracker = get_tracker()
    index = tracker._load_global_index()
    result = []
    for sid in index:
        data = tracker.session_total(sid)
        result.append({
            "session_id": sid,
            "total_cost_usd": data["total_cost_usd"],
            "total_calls": data["total_calls"],
            "total_input_tokens": data["total_input_tokens"],
            "total_output_tokens": data["total_output_tokens"],
        })
    return result

@api.get("/sessions/{session_id}/costs", response_model=dict)
async def get_session_costs(session_id: str, token: str = Depends(verify_token)):
    """Cost breakdown for a specific session."""
    from loophole.cost_tracker import get_tracker
    tracker = get_tracker()
    data = tracker.session_total(session_id)
    return data

@api.get("/sessions/{session_id}/costs/report")
async def get_session_cost_report(session_id: str, token: str = Depends(verify_token)):
    """Plain-text cost report for a session."""
    from loophole.cost_tracker import get_tracker
    tracker = get_tracker()
    return {"report": tracker.report_session(session_id)}

@api.get("/sessions/{session_id}/cost-summary", response_model=dict)
async def get_session_cost_summary(session_id: str, token: str = Depends(verify_token)):
    """Overall session cost stats: total, cost per round, cost per case."""
    from loophole.cost_tracker import get_tracker
    tracker = get_tracker()
    cost_data = tracker.session_total(session_id)

    # Load session state for round/case counts
    session_path = Path(SESSION_DIR) / session_id
    state_file = session_path / "state.json"
    current_round = 1
    case_count = 0
    if state_file.exists():
        try:
            import json as _json
            state = _json.loads(state_file.read_text())
            current_round = state.get("current_round", 1) or 1
            cases = state.get("cases", [])
            case_count = len(cases)
        except Exception:
            pass

    return {
        "session_id": session_id,
        "total_cost_usd": cost_data["total_cost_usd"],
        "total_calls": cost_data["total_calls"],
        "total_input_tokens": cost_data["total_input_tokens"],
        "total_output_tokens": cost_data["total_output_tokens"],
        "rounds": current_round,
        "cases": case_count,
        "cost_per_round_usd": round(cost_data["total_cost_usd"] / max(current_round, 1), 6),
        "cost_per_case_usd": round(cost_data["total_cost_usd"] / max(case_count, 1), 6),
    }

@api.get("/costs/all", response_model=dict)
async def get_all_session_costs(token: str = Depends(verify_token)):
    """All sessions cost summary — same as /costs/sessions but at the canonical path."""
    from loophole.cost_tracker import get_tracker
    tracker = get_tracker()
    index = tracker._load_global_index()
    result = []
    for sid in index:
        data = tracker.session_total(sid)
        result.append({
            "session_id": sid,
            "total_cost_usd": data["total_cost_usd"],
            "total_calls": data["total_calls"],
            "total_input_tokens": data["total_input_tokens"],
            "total_output_tokens": data["total_output_tokens"],
        })
    return {"sessions": result, "count": len(result)}}

@api.delete("/sessions/{session_id}")
async def delete_session(session_id: str, token: str = Depends(verify_token)):
    """Delete a session and all its data."""
    import shutil
    session_path = Path(SESSION_DIR) / session_id
    if not session_path.exists():
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    shutil.rmtree(session_path)
    # Also remove from SQLite if available
    if session_manager._sqlite:
        try:
            session_manager._sqlite._conn.execute(
                "DELETE FROM cases WHERE session_id = ?", (session_id,)
            )
            session_manager._sqlite._conn.execute(
                "DELETE FROM case_summaries WHERE session_id = ?", (session_id,)
            )
            session_manager._sqlite._conn.execute(
                "DELETE FROM responses WHERE session_id = ?", (session_id,)
            )
            session_manager._sqlite._conn.commit()
        except Exception:
            pass
    return {"ok": True, "deleted": session_id}

@api.post("/sessions/{session_id}/run")
async def run_session_round(session_id: str, rounds: int = 1, token: str = Depends(verify_token)):
    """
    Trigger one or more rounds of the adversarial loop.
    Note: Full agent-based loop is complex and requires the CLI run-loop.
    This endpoint runs a simplified synchronous loop for one round.
    """
    try:
        state = session_manager.load(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    try:
        from loophole.llm import LLMClient
        from loophole.agents.loophole_finder import LoopholeFinder
        from loophole.agents.overreach_finder import OverreachFinder
        from loophole.agents.judge import Judge
        from loophole.agents.legislator import LegislatorAgent
        from loophole.cost_tracker import get_tracker
        from loophole.deduplication import DeduplicationStore
        from loophole.models import RoundType
        import yaml

        with open("config.yaml") as f:
            config = yaml.safe_load(f)

        base_url = config["lite_llm"]["base_url"]
        api_key = config["lite_llm"]["api_key"]
        default_model = config["default_model"]
        max_tokens = config["max_tokens"]
        agent_models = config["agent_models"]
        temps = config["temperatures"]
        cases_per = config["loop"]["cases_per_agent"]

        def make_client(agent_name: str) -> LLMClient:
            model = agent_models.get(agent_name, default_model)
            return LLMClient(base_url=base_url, api_key=api_key, model=model, max_tokens=max_tokens, role=agent_name)

        loophole_finder = LoopholeFinder(make_client("loophole"), temperature=temps.get("loophole_finder", 0.9), cases_per_agent=cases_per)
        overreach_finder = OverreachFinder(make_client("overreach"), temperature=temps.get("overreach_finder", 0.9), cases_per_agent=cases_per)
        judge = Judge(make_client("judge"), temperature=temps.get("judge", 0.3))
        legislator = LegislatorAgent(make_client("legislator"), temperature=temps.get("legislator", 0.4))

        tracker = get_tracker()
        tracker.start_session(session_id)

        max_rounds = config["loop"]["max_rounds"]
        if state.current_round >= max_rounds:
            return {"session_id": session_id, "rounds_completed": 0, "error": "max rounds reached", "current_round": state.current_round}

        state.current_round += 1
        phases = [RoundType.OPENING, RoundType.ATTACK, RoundType.CLOSING]
        all_round_cases = []
        dedup_store = DeduplicationStore()

        for phase in phases:
            state.current_round_type = phase
            loopholes = loophole_finder.find(state, round_type=phase)
            overreaches = overreach_finder.find(state, round_type=phase)
            for c in loopholes + overreaches:
                fp = dedup_store.fingerprint(c.scenario, state.moral_principles)
                if dedup_store.is_duplicate(fp):
                    continue
                dedup_store.record(fp, state.session_id, c.id)
                all_round_cases.append(c)

        auto_resolved = 0
        escalated = 0
        for case_obj in all_round_cases:
            state.cases.append(case_obj)
            result = judge.evaluate(state, case_obj, round_type=case_obj.round_type)
            if result.resolvable and result.proposed_revision:
                case_obj.resolution = result.resolution_summary or result.reasoning
                case_obj.status = CaseStatus.AUTO_RESOLVED
                case_obj.resolved_by = "judge"
                revised = legislator.revise(state, case_obj)
                validation = judge.validate(state, revised.text)
                if validation.passes:
                    state.current_code = revised
                    auto_resolved += 1
                else:
                    case_obj.status = CaseStatus.ESCALATED
                    escalated += 1
            else:
                case_obj.status = CaseStatus.ESCALATED
                escalated += 1

        session_manager.save(state)
        return {
            "session_id": session_id,
            "rounds_completed": 1,
            "current_round": state.current_round,
            "cases_found": len(all_round_cases),
            "auto_resolved": auto_resolved,
            "escalated": escalated,
            "code_version": state.current_code.version,
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"Config or session file not found: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Run error: {str(e)}")
