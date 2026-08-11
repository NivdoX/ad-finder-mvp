from __future__ import annotations

import hashlib
import hmac
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask

from internal_outcomes import record_verified_basic_payment, register_internal_outcome_routes


class RecordingCursor:
    def __init__(self, database):
        self.database = database
        self.statements = []
        self._fetchone = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if normalized.startswith("INSERT INTO runningads_internal_request_receipts"):
            request_id = params[0]
            if request_id in self.database.receipts:
                self._fetchone = None
            else:
                self.database.receipts.add(request_id)
                self._fetchone = (request_id,)
        elif normalized.startswith("SELECT sequence, event_id"):
            after, limit = params
            self.database.selected = [row for row in self.database.rows if row[0] > after][:limit]

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return list(self.database.selected)


class RecordingConnection:
    def __init__(self, database):
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        cursor = RecordingCursor(self.database)
        self.database.cursors.append(cursor)
        return cursor

    def close(self):
        self.database.closes += 1


class FakeDatabase:
    def __init__(self):
        self.receipts = set()
        self.cursors = []
        self.selected = []
        self.closes = 0
        self.rows = [(
            1, "evt_verified_basic", "buyer@example.com", "basic", "paid",
            datetime(2026, 8, 11, 10, tzinfo=timezone.utc), "cus_1", "sub_1",
            "checkout.session.completed",
            {"stripe_signature_verified": True, "payment_status": "paid"},
        )]

    def connect(self):
        return RecordingConnection(self)


class RunningAdsOutcomeTests(unittest.TestCase):
    def test_only_verified_shape_basic_paid_checkout_is_recordable(self):
        database = FakeDatabase()
        cursor = RecordingCursor(database)
        event = {
            "id": "evt_1", "type": "checkout.session.completed", "created": 1786442400,
            "data": {"object": {
                "payment_status": "paid", "customer_email": " BUYER@EXAMPLE.COM ",
                "customer": "cus_1", "subscription": "sub_1", "metadata": {"plan": "basic"},
            }},
        }
        self.assertTrue(record_verified_basic_payment(cursor, event))
        insert = next(item for item in cursor.statements if item[0].startswith("INSERT INTO runningads_commercial_outcomes"))
        self.assertEqual(insert[1][0], "evt_1")
        self.assertEqual(insert[1][1], "buyer@example.com")
        self.assertIn('"stripe_signature_verified": true', insert[1][-1])
        for plan, state in (("pro", "paid"), ("basic", "unpaid")):
            rejected = {**event, "id": f"evt-{plan}-{state}", "data": {"object": {**event["data"]["object"], "payment_status": state, "metadata": {"plan": plan}}}}
            self.assertFalse(record_verified_basic_payment(cursor, rejected))

    def _headers(self, path, request_id):
        timestamp = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(b"").hexdigest()
        canonical = "\n".join(("GET", path, timestamp, request_id, digest, "")).encode()
        return {
            "X-NAEL-Timestamp": timestamp,
            "X-NAEL-Request-ID": request_id,
            "X-NAEL-Body-SHA256": digest,
            "X-NAEL-Signature": hmac.new(b"outcome-secret", canonical, hashlib.sha256).hexdigest(),
        }

    def test_read_only_hmac_feed_returns_versioned_exact_email_contract(self):
        database = FakeDatabase()
        app = Flask(__name__)
        register_internal_outcome_routes(app, database.connect)
        client = app.test_client()
        path = "/internal/v1/commercial-outcomes?after=0&limit=100"
        with patch.dict(os.environ, {"NAEL_RUNNINGADS_SHARED_SECRET": "outcome-secret"}):
            response = client.get(path, headers=self._headers(path, "request-1"))
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["next_cursor"], "1")
        event = payload["events"][0]
        self.assertEqual(event["customer_email"], "buyer@example.com")
        self.assertEqual(event["plan"], "basic")
        self.assertEqual(event["paid_state"], "paid")
        self.assertEqual(event["customer_id"], "cus_1")
        self.assertTrue(event["evidence"]["stripe_signature_verified"])
        self.assertEqual(client.post("/internal/v1/commercial-outcomes").status_code, 405)

    def test_invalid_signature_and_replay_fail_closed(self):
        database = FakeDatabase()
        app = Flask(__name__)
        register_internal_outcome_routes(app, database.connect)
        client = app.test_client()
        path = "/internal/v1/commercial-outcomes?after=0&limit=100"
        with patch.dict(os.environ, {"NAEL_RUNNINGADS_SHARED_SECRET": "outcome-secret"}):
            invalid = self._headers(path, "invalid")
            invalid["X-NAEL-Signature"] = "f" * 64
            self.assertEqual(client.get(path, headers=invalid).status_code, 401)
            valid = self._headers(path, "replay")
            self.assertEqual(client.get(path, headers=valid).status_code, 200)
            self.assertEqual(client.get(path, headers=valid).status_code, 409)


if __name__ == "__main__":
    unittest.main()
