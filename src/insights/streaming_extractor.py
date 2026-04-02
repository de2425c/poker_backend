"""Streaming concept extraction with carried state.

Processes textbook section-by-section, maintaining a running concept index
that allows the model to enrich existing concepts and link new ones.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from pypdf import PdfReader


@dataclass
class ConceptEntry:
    """Compact concept entry for the running index."""
    id: str
    name: str
    tags: list[str]  # Situation triggers + hand types combined
    insight: str     # One-liner
    chapters: list[str]  # Which chapters mention this

    def to_compact(self) -> str:
        """Compact string representation for context efficiency."""
        return f"[{self.id}] {self.name} | tags: {','.join(self.tags[:5])} | {self.insight[:80]}"


@dataclass
class FullConcept:
    """Full concept with all details - stored separately."""
    id: str
    name: str
    explanation: str
    when_applies: list[str]
    hand_types: list[str]
    key_insight: str
    source_quotes: list[str]  # Multiple quotes as concept gets enriched
    chapters: list[str]


EXTRACTION_PROMPT = """You are extracting poker concepts from a textbook for a coaching system.

## EXISTING CONCEPTS (may need updating/enriching)
{concept_index}

## CURRENT SECTION: {section_name}
{section_text}

## YOUR TASK
Analyze this section and return a JSON object with two arrays:

1. "updates": Existing concepts that should be enriched with info from this section
   - Only include if this section adds MEANINGFUL new context
   - Format: {{"id": "existing_id", "add_tags": [...], "add_quote": "...", "refined_insight": "..."}}

2. "new": Genuinely new concepts not already captured
   - Format: {{"id": "snake_case", "name": "...", "explanation": "...", "when_applies": [...], "hand_types": [...], "key_insight": "...", "source_quote": "..."}}

RULES:
- Don't create new concepts for ideas already in the index (update them instead)
- Tags should be specific situation triggers: "facing_3bet_IP", "cbet_wet_board", etc.
- key_insight is ONE sentence a coach would say
- Be selective - only extract distinct, actionable concepts

Return ONLY valid JSON: {{"updates": [...], "new": [...]}}
"""


def extract_sections_from_chapter(pdf_path: str, start_page: int, end_page: int) -> list[tuple[str, str]]:
    """Extract subsections from a chapter based on section headers.

    Returns list of (section_name, section_text) tuples.
    """
    reader = PdfReader(pdf_path)

    full_text = ""
    for i in range(start_page - 1, min(end_page, len(reader.pages))):
        full_text += reader.pages[i].extract_text() + "\n"

    # Split on section headers like "10.1", "10.2", etc.
    # Pattern matches: digit(s).digit(s) followed by title text
    pattern = r'(\d+\.\d+)\s+([A-Z][^\n]+)'

    sections = []
    matches = list(re.finditer(pattern, full_text))

    if not matches:
        # No subsections found, return whole chapter as one section
        return [("Full Chapter", full_text)]

    for i, match in enumerate(matches):
        section_num = match.group(1)
        section_title = match.group(2).strip()
        section_name = f"{section_num} {section_title}"

        start_pos = match.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)

        section_text = full_text[start_pos:end_pos].strip()

        # Only include sections with substantial content
        if len(section_text) > 500:
            sections.append((section_name, section_text))

    return sections


class StreamingConceptExtractor:
    """Extract concepts with carried state across sections."""

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-20250514"):
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model

        # Running state
        self.concept_index: dict[str, ConceptEntry] = {}  # Compact index
        self.full_concepts: dict[str, FullConcept] = {}   # Full details

    def _build_compact_index(self) -> str:
        """Build compact string representation of concept index."""
        if not self.concept_index:
            return "(No concepts extracted yet)"

        lines = []
        for concept in self.concept_index.values():
            lines.append(concept.to_compact())
        return "\n".join(lines)

    def _process_section(self, section_name: str, section_text: str) -> tuple[int, int]:
        """Process one section and update state.

        Returns (num_updates, num_new).
        """
        import time

        # Truncate section if too long (keep ~20k chars max)
        if len(section_text) > 20000:
            section_text = section_text[:20000] + "\n...[truncated]"

        prompt = EXTRACTION_PROMPT.format(
            concept_index=self._build_compact_index(),
            section_name=section_name,
            section_text=section_text
        )

        # Retry with exponential backoff for rate limits
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=2000,
                    messages=[{"role": "user", "content": prompt}]
                )
                break
            except Exception as e:
                if "rate_limit" in str(e).lower() or "429" in str(e):
                    wait_time = 30 * (2 ** attempt)  # 30s, 60s, 120s, 240s, 480s
                    print(f"  Rate limited, waiting {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    raise
        else:
            print(f"  Failed after {max_retries} retries")
            return 0, 0

        content = response.content[0].text

        # Parse JSON
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            print(f"  Warning: Failed to parse response for {section_name}")
            return 0, 0

        num_updates = 0
        num_new = 0

        # Process updates to existing concepts
        for update in result.get("updates", []):
            concept_id = update.get("id")
            if concept_id in self.concept_index:
                # Update compact index
                entry = self.concept_index[concept_id]
                if update.get("add_tags"):
                    entry.tags.extend(update["add_tags"])
                    entry.tags = list(set(entry.tags))[:10]  # Dedupe, limit
                if update.get("refined_insight"):
                    entry.insight = update["refined_insight"]
                entry.chapters.append(section_name)

                # Update full concept
                if concept_id in self.full_concepts:
                    full = self.full_concepts[concept_id]
                    if update.get("add_tags"):
                        full.when_applies.extend(update["add_tags"])
                        full.when_applies = list(set(full.when_applies))
                    if update.get("add_quote"):
                        full.source_quotes.append(update["add_quote"])
                    if update.get("refined_insight"):
                        full.key_insight = update["refined_insight"]
                    full.chapters.append(section_name)

                num_updates += 1

        # Process new concepts
        for new in result.get("new", []):
            concept_id = new.get("id")
            if not concept_id or concept_id in self.concept_index:
                continue

            # Add to compact index
            self.concept_index[concept_id] = ConceptEntry(
                id=concept_id,
                name=new.get("name", ""),
                tags=new.get("when_applies", []) + new.get("hand_types", []),
                insight=new.get("key_insight", ""),
                chapters=[section_name]
            )

            # Add full concept
            self.full_concepts[concept_id] = FullConcept(
                id=concept_id,
                name=new.get("name", ""),
                explanation=new.get("explanation", ""),
                when_applies=new.get("when_applies", []),
                hand_types=new.get("hand_types", []),
                key_insight=new.get("key_insight", ""),
                source_quotes=[new.get("source_quote", "")],
                chapters=[section_name]
            )

            num_new += 1

        return num_updates, num_new

    def extract_from_pdf(
        self,
        pdf_path: str,
        chapters: list[dict],
        output_path: str
    ) -> list[FullConcept]:
        """Extract concepts from all chapters with streaming state.

        Args:
            pdf_path: Path to PDF file
            chapters: List of {"name": str, "start": int, "end": int}
            output_path: Where to save the final concepts JSON
        """
        for chapter in chapters:
            print(f"\n{'='*60}")
            print(f"Chapter: {chapter['name']}")
            print(f"{'='*60}")

            sections = extract_sections_from_chapter(
                pdf_path,
                chapter["start"],
                chapter["end"]
            )

            for i, (section_name, section_text) in enumerate(sections):
                num_updates, num_new = self._process_section(section_name, section_text)
                print(f"  {section_name}: +{num_new} new, ~{num_updates} updated")
                # Small delay between sections to avoid rate limits
                if i < len(sections) - 1:
                    import time
                    time.sleep(3)

            print(f"  Running total: {len(self.concept_index)} concepts")

        # Save results
        output = [asdict(c) for c in self.full_concepts.values()]
        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2)

        print(f"\n{'='*60}")
        print(f"DONE: {len(output)} concepts saved to {output_path}")

        return list(self.full_concepts.values())


# Chapter definitions
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
