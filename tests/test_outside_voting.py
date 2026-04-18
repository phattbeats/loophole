"""Tests for outside voting system (PHA-193)."""
import pytest
from datetime import datetime
from loophole.models import Case, CaseType, CaseStatus, OutsideVote, VoteValue


class TestOutsideVoteModel:
    def test_outside_vote_create(self):
        vote = OutsideVote(
            voter_id="human_reviewer_1",
            vote=VoteValue.UPHOLD,
            confidence=4,
        )
        assert vote.voter_id == "human_reviewer_1"
        assert vote.vote == VoteValue.UPHOLD
        assert vote.confidence == 4
        assert isinstance(vote.voted_at, datetime)

    def test_vote_values(self):
        assert VoteValue.UPHOLD.value == "uphold"
        assert VoteValue.OVERTURN.value == "overturn"
        assert VoteValue.ABSTAIN.value == "abstain"

    def test_case_with_outside_votes(self):
        case = Case(
            id=1,
            round=1,
            case_type=CaseType.LOOPHOLE,
            scenario="Company X exploits tax loophole to avoid $10M in taxes",
            explanation="The loophole exists in section 26 CFR § 1.162-11",
            status=CaseStatus.PENDING,
            outside_votes=[],
        )
        assert case.outside_votes == []

        vote = OutsideVote(voter_id="reviewer_1", vote=VoteValue.OVERTURN, confidence=3)
        case.outside_votes.append(vote)

        assert len(case.outside_votes) == 1
        assert case.outside_votes[0].vote == VoteValue.OVERTURN
        assert case.outside_votes[0].confidence == 3

    def test_multiple_outside_votes(self):
        case = Case(
            id=2,
            round=1,
            case_type=CaseType.OVERREACH,
            scenario="Employee steals code and publishes it publicly",
            explanation="This violates copyright law but was done for whistleblowing",
            status=CaseStatus.ESCALATED,
            outside_votes=[
                OutsideVote(voter_id="judge_1", vote=VoteValue.UPHOLD, confidence=5),
                OutsideVote(voter_id="judge_2", vote=VoteValue.UPHOLD, confidence=4),
                OutsideVote(voter_id="observer_1", vote=VoteValue.ABSTAIN, confidence=2),
            ],
        )
        assert len(case.outside_votes) == 3
        votes_by_value = {v.vote for v in case.outside_votes}
        assert VoteValue.UPHOLD in votes_by_value
        assert VoteValue.ABSTAIN in votes_by_value


class TestVoteValidation:
    def test_confidence_bounds(self):
        from pydantic import ValidationError
        # confidence must be 1-5
        with pytest.raises(Exception):  # ValidationError
            OutsideVote(voter_id="x", vote=VoteValue.UPHOLD, confidence=0)
        with pytest.raises(Exception):
            OutsideVote(voter_id="x", vote=VoteValue.UPHOLD, confidence=6)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
