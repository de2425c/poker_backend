"""
Authentication service with Firebase Auth support.

Supports both development mode (mock tokens) and production mode (Firebase).
"""

import os
from typing import Optional

from ..models import PlayerIdentity
from .config import config
from .logging_config import logger


# Track if Firebase has been initialized (module-level singleton)
_firebase_initialized = False


def _initialize_firebase() -> bool:
    """
    Initialize Firebase Admin SDK if not already done.

    Uses Application Default Credentials (ADC) which works:
    - On Cloud Run: automatically uses the service account
    - Locally: uses GOOGLE_APPLICATION_CREDENTIALS env var pointing to service account JSON

    Returns True if initialization succeeded.
    """
    global _firebase_initialized

    if _firebase_initialized:
        return True

    try:
        import firebase_admin
        from firebase_admin import credentials

        # Check if already initialized by another instance
        try:
            firebase_admin.get_app()
            _firebase_initialized = True
            logger.info("Firebase already initialized")
            return True
        except ValueError:
            pass  # Not initialized yet

        # Check for explicit service account file
        creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        if creds_path and os.path.exists(creds_path):
            # Use explicit service account file
            cred = credentials.Certificate(creds_path)
            firebase_admin.initialize_app(cred)
            logger.info(f"Firebase initialized with service account from {creds_path}")
        else:
            # Use Application Default Credentials (works on Cloud Run)
            firebase_admin.initialize_app()
            logger.info("Firebase initialized with Application Default Credentials")

        _firebase_initialized = True
        return True

    except ImportError:
        logger.error("firebase-admin package not installed")
        return False
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")
        return False


class AuthService:
    """
    Authentication service supporting both dev and production modes.

    Dev mode (AUTH_ENABLED=false): Accepts "user_XXX" tokens directly.
    Production mode (AUTH_ENABLED=true): Verifies Firebase ID tokens.
    """

    def __init__(self):
        self._firebase_auth = None

        if config.auth_enabled:
            if _initialize_firebase():
                try:
                    from firebase_admin import auth as firebase_auth
                    self._firebase_auth = firebase_auth
                    logger.info("Firebase Auth ready")
                except ImportError:
                    logger.error("firebase-admin not installed but AUTH_ENABLED=true")
            else:
                logger.error("Firebase initialization failed, auth will reject all tokens")

    def verify_token(self, token: str) -> Optional[str]:
        """
        Verify token and return user_id, or None if invalid.

        In dev mode: accepts "user_XXX" tokens directly as user_id.
        In production: verifies Firebase ID token.
        """
        if not config.auth_enabled:
            # Dev mode: accept mock tokens
            if token.startswith("user_"):
                return token
            return None

        # Internal bot tokens bypass Firebase auth
        if token.startswith("user_bot_"):
            return token

        # Production mode: verify with Firebase
        if not self._firebase_auth:
            logger.error("Firebase Auth not available")
            return None

        try:
            decoded = self._firebase_auth.verify_id_token(token)
            user_id = decoded.get("uid")
            logger.info("Auth successful", user_id=user_id)
            return user_id
        except self._firebase_auth.InvalidIdTokenError as e:
            logger.warning(f"Invalid token: {e}")
            return None
        except self._firebase_auth.ExpiredIdTokenError:
            logger.warning("Expired token")
            return None
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return None

    def get_player_identity(self, user_id: str) -> PlayerIdentity:
        """
        Get player identity for a user.

        Dev mode: generates display name from user_id.
        Production: could fetch from Firestore user profile.
        """
        # Extract number from user_id like "user_0" -> "Player0"
        # In production, this could fetch real user data
        display_name = user_id.replace("user_", "Player")
        return PlayerIdentity(
            user_id=user_id,
            display_name=display_name,
            avatar_url=None,
        )
