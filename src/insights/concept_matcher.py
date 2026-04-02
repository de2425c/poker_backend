"""Match poker concepts to game situations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .concept_extractor import PokerConcept, load_concepts
from .schema import InsightRequest


# Situation tags derived from InsightRequest
def derive_situation_tags(request: InsightRequest) -> set[str]:
    """Derive situation tags from an insight request."""
    tags = set()

    # Street
    tags.add(request.street)

    # Position relationships
    hero_pos = request.hero_position.upper()
    villain_pos = request.villain_position.upper()
    tags.add(f"hero_{hero_pos}")
    tags.add(f"vs_{villain_pos}")

    # IP/OOP
    position_order = ["BB", "SB", "BTN", "CO", "HJ", "UTG"]
    hero_idx = position_order.index(hero_pos) if hero_pos in position_order else 3
    villain_idx = position_order.index(villain_pos) if villain_pos in position_order else 3
    if hero_idx < villain_idx:
        tags.add("hero_IP")
        tags.add("in_position")
    else:
        tags.add("hero_OOP")
        tags.add("out_of_position")

    # Stack depth
    if request.effective_stack_bb >= 80:
        tags.add("deep_stacked")
    elif request.effective_stack_bb <= 30:
        tags.add("short_stacked")
    else:
        tags.add("medium_stacked")

    # Action analysis
    action_str = " ".join(request.action_history).lower()
    if "3-bet" in action_str or "3bet" in action_str:
        tags.add("3bet_pot")
        tags.add("facing_3bet")
    if "4-bet" in action_str or "4bet" in action_str:
        tags.add("4bet_pot")
        tags.add("facing_4bet")
    if "raise" in action_str:
        tags.add("raised_pot")

    # Current action context
    opt_action = request.optimal_action.lower()
    if "bet" in opt_action or "raise" in opt_action:
        tags.add("betting")
        tags.add("aggression")
    if "check" in opt_action:
        tags.add("checking")
    if "fold" in opt_action:
        tags.add("folding")
    if "call" in opt_action:
        tags.add("calling")

    # Board texture (if provided)
    if request.board_texture:
        tags.add(f"board_{request.board_texture}")

    # Hand category
    if request.hand_category:
        tags.add(request.hand_category)

    # C-betting context
    if request.street == "flop" and "betting" in tags:
        tags.add("cbet")
        tags.add("continuation_bet")

    # Barrel context
    if request.street == "turn" and "betting" in tags:
        tags.add("turn_barrel")
        tags.add("double_barrel")
    if request.street == "river" and "betting" in tags:
        tags.add("river_barrel")
        tags.add("triple_barrel")

    return tags


def fuzzy_tag_match(situation_tags: set[str], concept_tags: set[str]) -> int:
    """Score how well concept tags match situation tags using fuzzy matching."""
    score = 0

    # Normalize all tags
    situation_normalized = {t.lower().replace("-", "_").replace(" ", "_") for t in situation_tags}
    concept_normalized = {t.lower().replace("-", "_").replace(" ", "_") for t in concept_tags}

    # Direct matches
    direct_matches = situation_normalized & concept_normalized
    score += len(direct_matches) * 3

    # Substring matches (e.g., "3bet" matches "facing_3bet_in_position")
    for stag in situation_normalized:
        for ctag in concept_normalized:
            if stag in ctag or ctag in stag:
                if stag != ctag:  # Don't double count direct matches
                    score += 1

    # Keyword matches
    keywords = {
        "3bet": ["three_bet", "3_bet", "3betting"],
        "facing_3bet": ["facing_three_bet", "vs_3bet"],
        "in_position": ["ip", "positional"],
        "out_of_position": ["oop"],
        "flop": ["postflop", "post_flop"],
        "checking": ["check", "passive"],
        "betting": ["bet", "aggressive", "aggression"],
        "deep_stacked": ["deep", "100bb"],
        "short_stacked": ["short", "shallow"],
        "squeeze": ["squeeze_spot", "squeezing"],
        "fold_equity": ["folding", "bluff"],
    }

    for stag in situation_normalized:
        if stag in keywords:
            for alias in keywords[stag]:
                for ctag in concept_normalized:
                    if alias in ctag:
                        score += 1

    return score


def match_concepts(
    request: InsightRequest,
    concepts: list[PokerConcept],
    min_score: int = 2,
    max_results: int = 3
) -> list[tuple[PokerConcept, int]]:
    """Find concepts that match the current situation.

    Returns list of (concept, match_score) tuples, sorted by relevance.
    """
    situation_tags = derive_situation_tags(request)

    scored_concepts = []

    for concept in concepts:
        # Build concept tags from when_applies and hand_types
        concept_tags = set(concept.when_applies + concept.hand_types)

        score = fuzzy_tag_match(situation_tags, concept_tags)

        if score >= min_score:
            scored_concepts.append((concept, score))

    # Sort by score descending
    scored_concepts.sort(key=lambda x: -x[1])

    return scored_concepts[:max_results]


class ConceptMatcher:
    """Match game situations to poker concepts."""

    def __init__(self, concepts_path: str | None = None):
        """Initialize with a concepts JSON file."""
        if concepts_path and Path(concepts_path).exists():
            self.concepts = load_concepts(concepts_path)
        else:
            self.concepts = []

    def find_relevant_concepts(
        self,
        request: InsightRequest,
        max_results: int = 3
    ) -> list[PokerConcept]:
        """Find concepts relevant to this situation."""
        if not self.concepts:
            return []

        matches = match_concepts(request, self.concepts, max_results=max_results)
        return [concept for concept, score in matches]

    def get_concept_context(self, request: InsightRequest) -> str:
        """Get concept context as a string for prompt injection."""
        concepts = self.find_relevant_concepts(request)

        if not concepts:
            return ""

        lines = ["Relevant poker concepts:"]
        for c in concepts:
            lines.append(f"\n**{c.name}**: {c.key_insight}")
            lines.append(f"  From: {c.source_chapter}")

        return "\n".join(lines)
