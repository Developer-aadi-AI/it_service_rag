"""Customer feedback (Great / OK / Poor) from the chat or from signed email links."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime
from typing import Callable, Literal

logger = logging.getLogger(__name__)

Rating = Literal["Great", "OK", "Poor"]
RATINGS: tuple[Rating, ...] = ("Great", "OK", "Poor")


class InvalidFeedback(ValueError):
    pass


def normalize_rating(value: str) -> Rating:
    for r in RATINGS:
        if (value or "").strip().lower() == r.lower():
            return r
    raise InvalidFeedback("rating must be one of Great, OK, Poor")


class FeedbackService:
    def __init__(self, db, clock: Callable[[], datetime], secret: str | None = None) -> None:
        self.db = db
        self.clock = clock
        if not secret:
            logger.warning("APP_SECRET_KEY not set: feedback links will stop working after a restart")
            secret = secrets.token_hex(32)
        self._secret = secret.encode("utf-8")

    # -- signed links ------------------------------------------------------------
    def token_for(self, session_id: str) -> str:
        sig = hmac.new(self._secret, session_id.encode(), hashlib.sha256).digest()[:16]
        raw = session_id.encode() + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def session_from_token(self, token: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            session_id, sig = raw.decode().split(".", 1)
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidFeedback("invalid feedback link") from exc
        if not hmac.compare_digest(self.token_for(session_id), token.rstrip("=")):
            raise InvalidFeedback("invalid feedback link")
        return session_id

    # -- storage -------------------------------------------------------------------
    def record(self, session_id: str, rating: str, comment: str | None = None, channel: str = "chat") -> Rating:
        r = normalize_rating(rating)
        comment = " ".join(comment.split())[:1000] if comment else None
        self.db.execute(
            "INSERT INTO feedback (session_id, created_at, rating, comment, channel) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment, "
            "channel=excluded.channel, created_at=excluded.created_at",
            (session_id, self.clock().isoformat(), r, comment, channel))
        logger.info("feedback session=%s rating=%s channel=%s", session_id[:8], r, channel)
        return r

    def get(self, session_id: str) -> dict | None:
        rows = self.db.query("SELECT * FROM feedback WHERE session_id = ?", (session_id,))
        return rows[0] if rows else None


FEEDBACK_THANKS = {
    "Great": "Thank you for the feedback - we're glad the chat was helpful!",
    "OK": "Thank you for the feedback - we'll keep working to make the chat more helpful.",
    "Poor": "Thank you for letting us know. We're sorry the chat didn't meet your expectations - "
            "your feedback helps us improve.",
}
