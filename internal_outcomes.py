"""Durable, read-only NAEL commercial outcome adapter for RunningAds."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

from flask import jsonify, request


MAX_CLOCK_SKEW = timedelta(minutes=5)


def ensure_outcome_schema(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS runningads_commercial_outcomes (
            sequence BIGSERIAL PRIMARY KEY,
            event_id TEXT UNIQUE NOT NULL,
            customer_email TEXT NOT NULL,
            plan TEXT NOT NULL,
            paid_state TEXT NOT NULL,
            event_timestamp TIMESTAMPTZ NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            source_event_type TEXT NOT NULL,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            acquisition_token TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        "ALTER TABLE runningads_commercial_outcomes ADD COLUMN IF NOT EXISTS acquisition_token TEXT"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS runningads_internal_request_receipts (
            request_id TEXT PRIMARY KEY,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS runningads_outcomes_created_idx "
        "ON runningads_commercial_outcomes (sequence)"
    )


def record_verified_basic_payment(cur, raw_event: dict) -> bool:
    """Persist only a Basic checkout that Stripe verified and marked paid."""
    if raw_event.get("type") != "checkout.session.completed":
        return False
    data = (raw_event.get("data") or {}).get("object") or {}
    plan = str((data.get("metadata") or {}).get("plan") or "").strip().lower()
    paid_state = str(data.get("payment_status") or "").strip().lower()
    email = str(
        data.get("customer_email")
        or (data.get("customer_details") or {}).get("email")
        or ""
    ).strip().lower()
    event_id = str(raw_event.get("id") or "").strip()
    occurred_at = _stripe_timestamp(raw_event.get("created"))
    if plan != "basic" or paid_state != "paid" or not email or not event_id or occurred_at is None:
        return False
    evidence = {
        "stripe_event_id": event_id,
        "stripe_event_type": raw_event["type"],
        "stripe_signature_verified": True,
        "payment_status": paid_state,
        "source": "RunningAds Stripe webhook",
    }
    acquisition_token = str((data.get("metadata") or {}).get("acquisition_token") or "").strip()
    if acquisition_token:
        evidence["acquisition_token"] = acquisition_token
    cur.execute(
        """
        INSERT INTO runningads_commercial_outcomes (
            event_id, customer_email, plan, paid_state, event_timestamp,
            stripe_customer_id, stripe_subscription_id, source_event_type, evidence, acquisition_token
        )
        VALUES (%s, %s, 'basic', 'paid', %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            event_id,
            email,
            occurred_at,
            data.get("customer"),
            data.get("subscription"),
            raw_event["type"],
            json.dumps(evidence, sort_keys=True),
            acquisition_token or None,
        ),
    )
    return True


def register_internal_outcome_routes(app, get_db_connection) -> None:
    @app.get("/internal/v1/commercial-outcomes")
    def runningads_commercial_outcomes():
        try:
            after = max(0, int(request.args.get("after") or 0))
            limit = max(1, min(100, int(request.args.get("limit") or 100)))
        except ValueError:
            return jsonify({"error": "Invalid outcome cursor"}), 400

        conn = get_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    ensure_outcome_schema(cur)
                    auth_error, auth_status = _authenticate(cur)
                    if auth_error:
                        return jsonify({"error": auth_error}), auth_status
                    cur.execute(
                        """
                        SELECT sequence, event_id, customer_email, plan, paid_state,
                               event_timestamp, stripe_customer_id,
                               stripe_subscription_id, source_event_type, evidence, acquisition_token
                        FROM runningads_commercial_outcomes
                        WHERE sequence > %s
                        ORDER BY sequence ASC
                        LIMIT %s
                        """,
                        (after, limit),
                    )
                    rows = cur.fetchall()
        finally:
            conn.close()

        events = [_outcome_contract(row) for row in rows]
        return jsonify(
            {
                "schema_version": "1.0",
                "events": events,
                "next_cursor": str(rows[-1][0] if rows else after),
            }
        )


def _authenticate(cur):
    secret = os.getenv("NAEL_RUNNINGADS_SHARED_SECRET", "").strip()
    timestamp = request.headers.get("X-NAEL-Timestamp", "")
    request_id = request.headers.get("X-NAEL-Request-ID", "")
    body_hash = request.headers.get("X-NAEL-Body-SHA256", "")
    signature = request.headers.get("X-NAEL-Signature", "")
    idempotency_key = request.headers.get("Idempotency-Key", "")
    if not all((secret, timestamp, request_id, body_hash, signature)):
        return "Missing HMAC authentication material", 401
    try:
        observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return "Invalid HMAC timestamp", 401
    if observed_at.tzinfo is None or abs(_now() - observed_at.astimezone(timezone.utc)) > MAX_CLOCK_SKEW:
        return "HMAC timestamp outside replay window", 401
    expected_body_hash = hashlib.sha256(request.get_data(cache=True)).hexdigest()
    if not hmac.compare_digest(expected_body_hash, body_hash):
        return "HMAC body hash mismatch", 401
    canonical = "\n".join(
        (request.method.upper(), request.full_path.rstrip("?"), timestamp, request_id, body_hash, idempotency_key)
    ).encode("utf-8")
    expected_signature = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        return "HMAC signature mismatch", 401
    cur.execute(
        "DELETE FROM runningads_internal_request_receipts WHERE received_at < NOW() - INTERVAL '1 hour'"
    )
    cur.execute(
        "INSERT INTO runningads_internal_request_receipts (request_id) VALUES (%s) "
        "ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
        (request_id,),
    )
    if cur.fetchone() is None:
        return "HMAC request replay detected", 409
    return "", 200


def _outcome_contract(row):
    evidence = row[9] if isinstance(row[9], dict) else json.loads(row[9] or "{}")
    return {
        "event_id": row[1],
        "schema_version": "1.0",
        "customer_email": row[2],
        "plan": row[3],
        "paid_state": row[4],
        "event_timestamp": row[5].astimezone(timezone.utc).isoformat(),
        "customer_id": row[6] or "",
        "subscription_id": row[7] or "",
        "evidence": evidence,
        "acquisition_token": row[10] or "",
        "provenance": {
            "service": "RunningAds",
            "source_event_type": row[8],
        },
    }


def _stripe_timestamp(value):
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _now():
    return datetime.now(timezone.utc)
