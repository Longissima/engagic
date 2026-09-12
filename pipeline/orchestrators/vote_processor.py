"""Compute recorded vote tallies without inferring a legal motion outcome."""
from typing import Any, Dict, List

from database.vote_utils import compute_vote_tally


class VoteProcessor:
    def process_votes(self, votes: List[Dict[str, Any]]) -> Dict[str, Any]:
        # Individual API ballots contain no recorded outcome. A majority is a
        # display estimate, not evidence of quorum or the required threshold.
        return {'tally': compute_vote_tally(votes), 'outcome': None}

    def compute_tally(self, votes: List[Dict[str, Any]]) -> Dict[str, int]:
        return compute_vote_tally(votes)
