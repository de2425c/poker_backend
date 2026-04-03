"""Vector store for poker concepts and textbook using Pinecone."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pinecone import Pinecone, ServerlessSpec


class PokerVectorStore:
    """Vector store with two namespaces: concepts and textbook chunks."""

    def __init__(self, index_name: str = "poker-rag"):
        """
        Initialize Pinecone vector store.

        Args:
            index_name: Name of the Pinecone index.
        """
        api_key = os.getenv("PINECONE_API_KEY")
        if not api_key:
            raise ValueError("PINECONE_API_KEY not set")

        self.pc = Pinecone(api_key=api_key)
        self.index_name = index_name
        self.model = "multilingual-e5-large"  # Pinecone's built-in embedding model

        # Create index if doesn't exist
        existing_indexes = [idx.name for idx in self.pc.list_indexes()]
        if index_name not in existing_indexes:
            print(f"Creating index '{index_name}'...")
            self.pc.create_index(
                name=index_name,
                dimension=1024,  # multilingual-e5-large dimension
                metric="cosine",
                spec=ServerlessSpec(cloud="aws", region="us-east-1")
            )

        self.index = self.pc.Index(index_name)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using Pinecone's inference API with retry."""
        import time

        max_retries = 5
        for attempt in range(max_retries):
            try:
                embeddings = self.pc.inference.embed(
                    model=self.model,
                    inputs=texts,
                    parameters={"input_type": "passage"}
                )
                return [e.values for e in embeddings.data]
            except Exception as e:
                if "429" in str(e) or "rate" in str(e).lower():
                    wait = 30 * (2 ** attempt)
                    print(f"    Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        raise Exception("Max retries exceeded")

    def _embed_query(self, query: str) -> list[float]:
        """Embed a query using Pinecone's inference API."""
        embeddings = self.pc.inference.embed(
            model=self.model,
            inputs=[query],
            parameters={"input_type": "query"}
        )
        return embeddings.data[0].values

    def index_concepts(self, concepts_path: str):
        """
        Index concepts from JSON file.

        Args:
            concepts_path: Path to concepts JSON file.
        """
        with open(concepts_path) as f:
            concepts = json.load(f)

        # Build documents
        docs = []
        for c in concepts:
            doc = f"{c['name']}. {c['key_insight']}"
            if c.get("explanation"):
                doc += f" {c['explanation']}"
            docs.append({
                "id": c["id"],
                "text": doc,
                "metadata": {
                    "type": "concept",
                    "name": c["name"],
                    "insight": c["key_insight"][:500],  # Pinecone metadata limit
                    "chapter": c.get("source_chapter", "")[:100],
                }
            })

        # Embed in batches
        batch_size = 96
        for i in range(0, len(docs), batch_size):
            batch = docs[i:i + batch_size]
            texts = [d["text"] for d in batch]
            embeddings = self._embed(texts)

            vectors = [
                {
                    "id": f"concept_{d['id']}",
                    "values": emb,
                    "metadata": d["metadata"]
                }
                for d, emb in zip(batch, embeddings)
            ]

            self.index.upsert(vectors=vectors, namespace="concepts")
            print(f"  Indexed concepts {i+1}-{min(i+batch_size, len(docs))}")

        print(f"Indexed {len(docs)} concepts")

    def index_textbook(self, pdf_path: str, chapters: list[dict]):
        """
        Index textbook chunks from PDF.

        Args:
            pdf_path: Path to PDF file.
            chapters: List of {"name": str, "start": int, "end": int}.
        """
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)

        chunks = []
        chunk_id = 0

        for chapter in chapters:
            chapter_name = chapter["name"]
            start_page = chapter["start"] - 1
            end_page = min(chapter["end"], len(reader.pages))

            # Extract chapter text
            chapter_text = ""
            for i in range(start_page, end_page):
                chapter_text += reader.pages[i].extract_text() + "\n"

            # Split into chunks
            chunk_size = 1500
            overlap = 150

            for i in range(0, len(chapter_text), chunk_size - overlap):
                chunk = chapter_text[i:i + chunk_size].strip()
                if len(chunk) < 100:
                    continue

                chunk_id += 1
                chunks.append({
                    "id": f"chunk_{chunk_id}",
                    "text": chunk,
                    "metadata": {
                        "type": "textbook",
                        "chapter": chapter_name,
                        "start_page": chapter["start"],
                    }
                })

        # Embed in batches
        batch_size = 96
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            texts = [c["text"] for c in batch]
            embeddings = self._embed(texts)

            vectors = [
                {
                    "id": c["id"],
                    "values": emb,
                    "metadata": {**c["metadata"], "text": c["text"][:1000]}  # Store truncated text
                }
                for c, emb in zip(batch, embeddings)
            ]

            self.index.upsert(vectors=vectors, namespace="textbook")
            print(f"  Indexed chunks {i+1}-{min(i+batch_size, len(chunks))}")

        print(f"Indexed {len(chunks)} textbook chunks")

    def search_concepts(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Search for relevant concepts.

        Returns:
            List of concept dicts with id, name, insight, score.
        """
        query_embedding = self._embed_query(query)

        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace="concepts",
            include_metadata=True
        )

        return [
            {
                "id": m.id.replace("concept_", ""),
                "name": m.metadata.get("name", ""),
                "insight": m.metadata.get("insight", ""),
                "chapter": m.metadata.get("chapter", ""),
                "score": m.score
            }
            for m in results.matches
        ]

    def search_textbook(self, query: str, top_k: int = 3) -> list[dict]:
        """
        Search for relevant textbook passages.

        Returns:
            List of dicts with text, chapter, score.
        """
        query_embedding = self._embed_query(query)

        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace="textbook",
            include_metadata=True
        )

        return [
            {
                "text": m.metadata.get("text", ""),
                "chapter": m.metadata.get("chapter", ""),
                "score": m.score
            }
            for m in results.matches
        ]

    def search(self, query: str, n_concepts: int = 3, n_textbook: int = 2) -> dict:
        """
        Search both concepts and textbook.

        Returns:
            {"concepts": [...], "textbook": [...]}
        """
        return {
            "concepts": self.search_concepts(query, n_concepts),
            "textbook": self.search_textbook(query, n_textbook)
        }


def build_situation_query(request) -> str:
    """Build a searchable query from an InsightRequest."""
    parts = []

    # Core decision context
    street = request.street
    opt = request.optimal_action.lower() if request.optimal_action else ""

    # Describe the decision type
    if street == "preflop":
        if "3bet" in " ".join(request.action_history).lower():
            parts.append("facing 3-bet decision")
        elif "4bet" in opt:
            parts.append("4-betting strategy")
        else:
            parts.append("preflop opening and calling")
    elif street == "flop":
        if "bet" in opt:
            parts.append("c-betting continuation bet flop strategy")
        elif "check" in opt:
            parts.append("checking back flop pot control")
        elif "call" in opt or "fold" in opt:
            parts.append("facing bet on flop defense")
    elif street == "turn":
        if "bet" in opt:
            parts.append("turn barrel double barrel")
        else:
            parts.append("turn defense and pot control")
    elif street == "river":
        if "bet" in opt:
            parts.append("river value bet or bluff")
        else:
            parts.append("river defense bluff catching")

    # Position context
    parts.append(f"{request.hero_position} vs {request.villain_position}")

    # Hand category if available
    if hasattr(request, 'hand_category') and request.hand_category:
        parts.append(request.hand_category)

    # Board texture
    if hasattr(request, 'board_texture') and request.board_texture:
        parts.append(f"{request.board_texture} board texture")

    # Hand description
    if request.hero_hand:
        hand = request.hero_hand
        # Describe hand type
        if hand[0] == hand[2]:  # Pocket pair
            parts.append("pocket pair")
        elif hand[1] == hand[3]:  # Suited
            parts.append("suited hand")
        if 'A' in hand:
            parts.append("ace high")

    return ". ".join(parts)


# Chapter definitions for indexing
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
