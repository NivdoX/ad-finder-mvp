# NAEL commercial outcome feed

RunningAds now records a durable commercial outcome only after its existing Stripe webhook signature verification succeeds and the event proves all of the following:

- event type is `checkout.session.completed`;
- metadata plan is exactly `basic`;
- Stripe payment status is exactly `paid`;
- an exact normalized customer email and stable Stripe event ID exist.

The event is inserted idempotently by Stripe event ID. Pro, unpaid, visitor, signup, click, reply, and link-request activity is not exposed as a paying-customer outcome.

`GET /internal/v1/commercial-outcomes?after=<sequence>&limit=<1..100>` is read-only and requires HMAC-SHA256 authentication using `NAEL_RUNNINGADS_SHARED_SECRET`. Authentication includes timestamp, request ID, empty-body digest, full path/query, and a durable replay receipt. The response is a version-1 JSON contract with stable event ID, exact email, plan, paid state, event timestamp, customer/subscription IDs, and Stripe-verification provenance.

The adapter creates no customer, subscription, payment, or Stripe request. It reads only the ledger produced by the already verified webhook path. NAEL remains responsible for exact-email attribution, deduplication, ambiguity handling, and stopping future acquisition work.
