"""Extract poker concepts from textbook PDFs using Claude."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from pypdf import PdfReader


@dataclass
class PokerConcept:
    """A poker concept extracted from educational material."""

    id: str
    name: str
    explanation: str
    when_applies: list[str]  # Situation triggers
    hand_types: list[str]    # Applicable hand categories
    key_insight: str         # Coach's one-liner
    source_quote: str        # Direct quote from source
    source_chapter: str = ""
    source_page: int = 0


EXTRACTION_PROMPT = """You are extracting poker concepts from a textbook chapter for a coaching RAG system.

For each distinct concept, output a JSON object with:
- id: short snake_case identifier
- name: human-readable name (5-10 words)
- explanation: 2-3 sentence explanation a player could understand
- when_applies: list of situation triggers (be specific, e.g., "facing_3bet_IP", "cbet_dry_board", "turn_barrel_scare_card")
- hand_types: list of hand categories (e.g., "suited_aces", "pocket_pairs", "top_pair", "flush_draws")
- key_insight: one sentence a coach would say to a student
- source_quote: a short direct quote from the text

Output ONLY a valid JSON array. Extract 5-10 core concepts per chapter section.

CHAPTER TEXT:
"""


def extract_chapter(pdf_path: str, start_page: int, end_page: int) -> str:
    """Extract text from a range of pages."""
    reader = PdfReader(pdf_path)
    text = ""
    for i in range(start_page - 1, min(end_page, len(reader.pages))):
        text += reader.pages[i].extract_text() + "\n\n"
    return text


def extract_concepts_from_text(
    text: str,
    chapter_name: str,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-20250514"
) -> list[PokerConcept]:
    """Use Claude to extract structured concepts from chapter text."""
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    # Chunk if too long (roughly 30k chars = ~8k tokens)
    max_chunk = 30000
    chunks = [text[i:i+max_chunk] for i in range(0, len(text), max_chunk)]

    all_concepts = []

    for i, chunk in enumerate(chunks):
        response = client.messages.create(
            model=model,
            max_tokens=3000,
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT + chunk
            }]
        )

        # Parse JSON from response
        content = response.content[0].text
        # Handle markdown code blocks
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        try:
            concepts_data = json.loads(content)
            for c in concepts_data:
                concept = PokerConcept(
                    id=c["id"],
                    name=c["name"],
                    explanation=c["explanation"],
                    when_applies=c["when_applies"],
                    hand_types=c["hand_types"],
                    key_insight=c["key_insight"],
                    source_quote=c["source_quote"],
                    source_chapter=chapter_name
                )
                all_concepts.append(concept)
        except json.JSONDecodeError as e:
            print(f"Failed to parse chunk {i}: {e}")
            continue

    return all_concepts


# Chapter definitions for The Grinder's Manual
GRINDERS_MANUAL_CHAPTERS = [
    {"name": "Opening the Pot", "start": 18, "end": 50},
    {"name": "ISO Raises", "start": 50, "end": 80},
    {"name": "C-Betting", "start": 80, "end": 120},
    {"name": "Value Betting", "start": 120, "end": 160},
    {"name": "Calling Opens", "start": 160, "end": 220},
    {"name": "Facing Bets - End of Action", "start": 220, "end": 260},
    {"name": "Facing Bets - Open Action", "start": 260, "end": 300},
    {"name": "Combos and Blockers", "start": 300, "end": 340},
    {"name": "3-Betting", "start": 340, "end": 390},
    {"name": "Facing 3-Bets", "start": 390, "end": 440},
    {"name": "Bluffing Turn and River", "start": 440, "end": 490},
]


def extract_all_concepts(
    pdf_path: str,
    output_path: str,
    chapters: list[dict] | None = None,
    api_key: str | None = None
) -> list[PokerConcept]:
    """Extract concepts from all chapters and save to JSON."""
    chapters = chapters or GRINDERS_MANUAL_CHAPTERS
    all_concepts = []

    for chapter in chapters:
        print(f"Extracting: {chapter['name']} (pages {chapter['start']}-{chapter['end']})")

        text = extract_chapter(pdf_path, chapter["start"], chapter["end"])
        concepts = extract_concepts_from_text(text, chapter["name"], api_key)

        print(f"  Found {len(concepts)} concepts")
        all_concepts.extend(concepts)

    # Save to JSON
    with open(output_path, 'w') as f:
        json.dump([asdict(c) for c in all_concepts], f, indent=2)

    print(f"\nTotal: {len(all_concepts)} concepts saved to {output_path}")
    return all_concepts


def load_concepts(path: str) -> list[PokerConcept]:
    """Load concepts from JSON file."""
    with open(path) as f:
        data = json.load(f)

    concepts = []
    for c in data:
        # Handle both streaming extractor format (source_quotes, chapters)
        # and simple extractor format (source_quote, source_chapter)
        if "source_quotes" in c:
            c["source_quote"] = c.pop("source_quotes")[0] if c["source_quotes"] else ""
        if "chapters" in c and "source_chapter" not in c:
            c["source_chapter"] = c.pop("chapters")[0] if c["chapters"] else ""
        elif "chapters" in c:
            c.pop("chapters")  # Remove extra field

        concepts.append(PokerConcept(**c))

    return concepts
