"""PII-free signed acquisition token verification for RunningAds."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone


def verify_acquisition_token(token: str, secret: str, *, now: datetime | None = None) -> dict | None:
    if not token or not secret:
        return None
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if set(payload) != {"jti", "exp"} or not str(payload.get("jti", "")).startswith("acq_"):
        return None
    now = now or datetime.now(timezone.utc)
    if int(payload.get("exp", 0)) < int(now.timestamp()):
        return None
    return {"token_id": payload["jti"], "expires_at": int(payload["exp"])}


def checkout_metadata(user_id: str, plan: str, token: str, secret: str) -> dict[str, str]:
    metadata = {"user_id": str(user_id), "plan": str(plan)}
    if verify_acquisition_token(token, secret):
        metadata["acquisition_token"] = token
    return metadata
