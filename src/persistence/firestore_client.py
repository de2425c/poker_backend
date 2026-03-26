"""
Firestore client wrapper with in-memory fallback.

Uses Firebase Admin SDK when available, falls back to in-memory storage for testing.
"""

import logging
from typing import Optional
from .models import HandLog, LedgerEntry

logger = logging.getLogger(__name__)


class FirestoreClient:
    """
    Firestore wrapper with in-memory fallback for testing.

    Attempts to connect to Firestore using Firebase Admin SDK.
    Falls back to in-memory storage if Firebase is not configured.
    """

    def __init__(self, use_memory: bool = False):
        """
        Initialize Firestore client.

        Args:
            use_memory: Force in-memory mode (for testing)
        """
        self._db = None
        self._in_memory: dict[str, list] = {"hands": [], "ledger": []}

        if not use_memory:
            self._try_init_firestore()

    def _try_init_firestore(self) -> None:
        """Attempt to initialize Firebase Admin SDK."""
        try:
            import firebase_admin
            from firebase_admin import firestore

            # Initialize Firebase if not already done
            # Expects GOOGLE_APPLICATION_CREDENTIALS env var
            if not firebase_admin._apps:
                logger.info("Initializing Firebase Admin SDK...")
                firebase_admin.initialize_app()

            self._db = firestore.client()
            logger.info("Firestore client initialized successfully")
        except Exception as e:
            # Fall back to in-memory mode
            logger.warning(f"Firestore initialization failed, using in-memory storage: {e}")

    @property
    def is_connected(self) -> bool:
        """Check if connected to Firestore."""
        return self._db is not None

    async def write_hand_log(self, hand_log: HandLog) -> None:
        """
        Write hand log to storage.

        Args:
            hand_log: Complete hand history to persist
        """
        data = hand_log.to_dict()

        if self._db:
            logger.info(f"Writing hand {hand_log.hand_id} to Firestore")
            self._db.collection("hands").document(hand_log.hand_id).set(data)
            logger.info(f"Hand {hand_log.hand_id} written successfully")
        else:
            logger.warning(f"Writing hand {hand_log.hand_id} to in-memory (Firestore not connected)")
            self._in_memory["hands"].append(data)

    async def write_ledger_entries(self, entries: list[LedgerEntry]) -> None:
        """
        Write ledger entries to storage.

        Args:
            entries: List of chip movement records
        """
        if not entries:
            return

        if self._db:
            logger.info(f"Writing {len(entries)} ledger entries to Firestore")
            for entry in entries:
                data = entry.to_dict()
                self._db.collection("ledger").document(entry.entry_id).set(data)
            logger.info(f"Ledger entries written successfully")
        else:
            logger.warning(f"Writing {len(entries)} ledger entries to in-memory (Firestore not connected)")
            for entry in entries:
                data = entry.to_dict()
                self._in_memory["ledger"].append(data)

    def get_hand_log(self, hand_id: str) -> Optional[dict]:
        """
        Retrieve hand log by ID.

        Args:
            hand_id: Unique hand identifier

        Returns:
            Hand log data or None if not found
        """
        if self._db:
            doc = self._db.collection("hands").document(hand_id).get()
            return doc.to_dict() if doc.exists else None
        else:
            for h in self._in_memory["hands"]:
                if h["hand_id"] == hand_id:
                    return h
            return None

    def get_ledger_entries(self, user_id: str, hand_id: Optional[str] = None) -> list[dict]:
        """
        Retrieve ledger entries for a user.

        Args:
            user_id: User to look up
            hand_id: Optional filter by hand

        Returns:
            List of ledger entry dicts
        """
        if self._db:
            query = self._db.collection("ledger").where("user_id", "==", user_id)
            if hand_id:
                query = query.where("hand_id", "==", hand_id)
            return [doc.to_dict() for doc in query.stream()]
        else:
            entries = []
            for e in self._in_memory["ledger"]:
                if e["user_id"] == user_id:
                    if hand_id is None or e["hand_id"] == hand_id:
                        entries.append(e)
            return entries

    def get_all_hand_logs(self) -> list[dict]:
        """
        Retrieve all hand logs (for debugging).

        Returns:
            List of all hand log dicts
        """
        if self._db:
            return [doc.to_dict() for doc in self._db.collection("hands").stream()]
        else:
            return list(self._in_memory["hands"])

    def get_all_ledger_entries(self) -> list[dict]:
        """
        Retrieve all ledger entries (for debugging).

        Returns:
            List of all ledger entry dicts
        """
        if self._db:
            return [doc.to_dict() for doc in self._db.collection("ledger").stream()]
        else:
            return list(self._in_memory["ledger"])

    def clear(self) -> None:
        """Clear in-memory storage (for testing)."""
        self._in_memory = {"hands": [], "ledger": []}

    # =========================================================================
    # WALLET OPERATIONS
    # =========================================================================

    async def get_user_balance(self, user_id: str) -> int:
        """
        Get user balance in cents.

        Args:
            user_id: User to look up

        Returns:
            Balance in cents (0 if wallet doesn't exist)

        Note:
            Firestore stores balance as 'dollars' (whole numbers).
            We convert to cents: dollars * 100 = cents
        """
        if self._db:
            doc = self._db.collection("wallets").document(user_id).get()
            if doc.exists:
                data = doc.to_dict()
                dollars = data.get("dollars", 0)
                return dollars * 100
            return 0
        else:
            # In-memory mode
            wallets = self._in_memory.get("wallets", {})
            dollars = wallets.get(user_id, 0)
            return dollars * 100

    async def deduct_balance(self, user_id: str, cents: int) -> int:
        """
        Deduct cents from user balance atomically.

        Args:
            user_id: User to deduct from
            cents: Amount to deduct in cents

        Returns:
            New balance in cents after deduction

        Raises:
            ValueError: If insufficient funds
        """
        if cents <= 0:
            raise ValueError("Deduction amount must be positive")

        if self._db:
            from google.cloud.firestore import transactional
            from google.cloud import firestore as firestore_module

            transaction = self._db.transaction()
            wallet_ref = self._db.collection("wallets").document(user_id)

            @transactional
            def deduct_in_transaction(txn, ref):
                doc = ref.get(transaction=txn)
                if not doc.exists:
                    raise ValueError(f"Wallet not found for user {user_id}")

                data = doc.to_dict()
                current_dollars = data.get("dollars", 0)
                current_cents = current_dollars * 100

                if current_cents < cents:
                    raise ValueError(
                        f"Insufficient balance: {current_cents} cents < {cents} cents"
                    )

                new_cents = current_cents - cents
                new_dollars = new_cents // 100
                txn.update(ref, {"dollars": new_dollars})
                return new_cents

            new_balance = deduct_in_transaction(transaction, wallet_ref)
            logger.info(f"Deducted {cents} cents from {user_id}, new balance: {new_balance}")
            return new_balance
        else:
            # In-memory mode
            if "wallets" not in self._in_memory:
                self._in_memory["wallets"] = {}

            wallets = self._in_memory["wallets"]
            current_dollars = wallets.get(user_id, 0)
            current_cents = current_dollars * 100

            if current_cents < cents:
                raise ValueError(
                    f"Insufficient balance: {current_cents} cents < {cents} cents"
                )

            new_cents = current_cents - cents
            new_dollars = new_cents // 100
            wallets[user_id] = new_dollars
            logger.info(f"[In-Memory] Deducted {cents} cents from {user_id}, new balance: {new_cents}")
            return new_cents

    async def add_balance(self, user_id: str, cents: int) -> int:
        """
        Add cents to user balance atomically.

        Args:
            user_id: User to credit
            cents: Amount to add in cents

        Returns:
            New balance in cents after addition
        """
        if cents <= 0:
            raise ValueError("Addition amount must be positive")

        if self._db:
            from google.cloud.firestore import transactional
            from google.cloud import firestore as firestore_module

            transaction = self._db.transaction()
            wallet_ref = self._db.collection("wallets").document(user_id)

            @transactional
            def add_in_transaction(txn, ref):
                doc = ref.get(transaction=txn)
                if doc.exists:
                    data = doc.to_dict()
                    current_dollars = data.get("dollars", 0)
                else:
                    current_dollars = 0

                current_cents = current_dollars * 100
                new_cents = current_cents + cents
                new_dollars = new_cents // 100

                if doc.exists:
                    txn.update(ref, {"dollars": new_dollars})
                else:
                    txn.set(ref, {"dollars": new_dollars})

                return new_cents

            new_balance = add_in_transaction(transaction, wallet_ref)
            logger.info(f"Added {cents} cents to {user_id}, new balance: {new_balance}")
            return new_balance
        else:
            # In-memory mode
            if "wallets" not in self._in_memory:
                self._in_memory["wallets"] = {}

            wallets = self._in_memory["wallets"]
            current_dollars = wallets.get(user_id, 0)
            current_cents = current_dollars * 100
            new_cents = current_cents + cents
            new_dollars = new_cents // 100
            wallets[user_id] = new_dollars
            logger.info(f"[In-Memory] Added {cents} cents to {user_id}, new balance: {new_cents}")
            return new_cents
