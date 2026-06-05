"""
RunningAds Opportunity Agent.

Internal-only usage:
    /admin/opportunities?secret=YOUR_SECRET

This module only reads public Reddit JSON endpoints. It does not post, comment,
send DMs, create accounts, or automate Reddit engagement.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import requests
from openai import OpenAI


SUBREDDITS = [
    "FacebookAds",
    "PPC",
    "shopify",
    "ecommerce",
    "dropshipping",
    "EntrepreneurRideAlong",
    "marketing",
    "digital_marketing",
]

KEYWORDS = [
    "competitor ads",
    "competitors ads",
    "facebook ads",
    "meta ads",
    "ad library",
    "ads library",
    "winning ads",
    "ad creative",
    "creatives",
    "ugc ads",
    "ecommerce ads",
    "shopify ads",
    "ad fatigue",
    "roas",
    "cpa",
    "ctr",
    "media buyer",
    "performance marketing",
    "dropshipping ads",
    "spy tool",
    "ad spy",
    "creative testing",
]

LOW_RELEVANCE_TERMS = [
    "crypto",
    "hiring",
    "job posting",
    "for hire",
    "meme",
    "motivation",
    "personal finance",
    "coding",
    "developer",
]

REQUEST_TIMEOUT_SECONDS = 8
CACHE_TTL_SECONDS = 15 * 60
DEFAULT_LIMIT_PER_SUBREDDIT = 12
MAX_AI_CANDIDATES = 18

_cache = {}
_cache_lock = threading.Lock()


def fetch_opportunities(selected_subreddit=None, force_refresh=False):
    selected_subreddit = (selected_subreddit or "").strip()
    cache_key = selected_subreddit or "all"
    now = time.time()

    with _cache_lock:
        cached = _cache.get(cache_key)
        if (
            cached
            and not force_refresh
            and now - cached["cached_at"] < CACHE_TTL_SECONDS
        ):
            data = dict(cached["data"])
            data["cached"] = True
            return data

    if selected_subreddit and selected_subreddit not in SUBREDDITS:
        return {
            "opportunities": [],
            "errors": [f"Unsupported subreddit: {selected_subreddit}"],
            "subreddits": SUBREDDITS,
            "selected_subreddit": selected_subreddit,
            "cached": False,
            "fetched_at": datetime.now(timezone.utc),
            "openai_enabled": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        }

    subreddits = [selected_subreddit] if selected_subreddit else SUBREDDITS
    posts = []
    errors = []

    for subreddit in subreddits:
        try:
            posts.extend(_fetch_subreddit_posts(subreddit))
        except Exception as exc:
            errors.append(f"r/{subreddit}: {str(exc)}")
        time.sleep(0.2)

    candidates = [_prepare_candidate(post) for post in posts]
    candidates = [item for item in candidates if item]
    candidates.sort(
        key=lambda item: (item["relevance_score"], item["comments"], item["upvotes"]),
        reverse=True,
    )
    candidates = candidates[:MAX_AI_CANDIDATES]

    opportunities = _score_with_openai(candidates, errors)
    opportunities.sort(
        key=lambda item: (item["relevance_score"], item["comments"], item["upvotes"]),
        reverse=True,
    )

    data = {
        "opportunities": opportunities,
        "errors": errors,
        "subreddits": SUBREDDITS,
        "selected_subreddit": selected_subreddit,
        "cached": False,
        "fetched_at": datetime.now(timezone.utc),
        "openai_enabled": bool(os.getenv("OPENAI_API_KEY", "").strip()),
    }

    with _cache_lock:
        _cache[cache_key] = {"cached_at": now, "data": data}

    return data


def _fetch_subreddit_posts(subreddit):
    url = f"https://www.reddit.com/r/{subreddit}/new.json"
    response = requests.get(
        url,
        params={"limit": DEFAULT_LIMIT_PER_SUBREDDIT},
        headers={
            "User-Agent": "RunningAdsOpportunityAgent/0.1 internal discovery contact@getrunningads.com"
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    children = payload.get("data", {}).get("children", [])
    return [child.get("data", {}) for child in children if child.get("data")]


def _prepare_candidate(post):
    title = (post.get("title") or "").strip()
    body = (post.get("selftext") or "").strip()
    text = f"{title}\n{body}".lower()
    matched_keywords = [keyword for keyword in KEYWORDS if keyword in text]

    if not matched_keywords:
        return None

    rule_score, explanation = _rule_score(text, matched_keywords)
    if rule_score < 3:
        return None

    created_utc = post.get("created_utc") or 0
    permalink = post.get("permalink") or ""
    post_url = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")

    return {
        "id": post.get("id") or post_url or title,
        "subreddit": post.get("subreddit") or "",
        "title": title,
        "url": post_url,
        "author": post.get("author") or "unknown",
        "age": _format_age(created_utc),
        "created_utc": created_utc,
        "upvotes": int(post.get("score") or 0),
        "comments": int(post.get("num_comments") or 0),
        "relevance_score": rule_score,
        "explanation": explanation,
        "suggested_reply": _rule_reply(text),
        "matched_keywords": matched_keywords,
    }


def _rule_score(text, matched_keywords):
    score = 0
    weights = {
        "competitor ads": 4,
        "competitors ads": 4,
        "ad spy": 4,
        "spy tool": 4,
        "winning ads": 4,
        "ad library": 4,
        "ads library": 4,
        "meta ads": 3,
        "facebook ads": 3,
        "ad fatigue": 3,
        "creative testing": 3,
        "ad creative": 2,
        "creatives": 2,
        "performance marketing": 2,
        "media buyer": 2,
        "ecommerce ads": 2,
        "shopify ads": 2,
        "dropshipping ads": 2,
        "roas": 1,
        "cpa": 1,
        "ctr": 1,
        "ugc ads": 1,
    }

    for keyword in matched_keywords:
        score += weights.get(keyword, 1)

    if any(term in text for term in ["how do i find", "where can i find", "research competitors", "before launching"]):
        score += 2
    if any(term in text for term in ["not working", "stopped working", "fatigue", "burned out"]):
        score += 1
    if any(term in text for term in LOW_RELEVANCE_TERMS):
        score -= 3

    score = max(0, min(10, score))
    explanation = "Matched: " + ", ".join(matched_keywords[:4])
    if score >= 8:
        explanation += ". Strong fit for competitor ad research or creative discovery."
    elif score >= 5:
        explanation += ". Potential fit for a helpful, non-promotional response."
    else:
        explanation += ". Weak but possibly relevant after human review."
    return score, explanation


def _rule_reply(text):
    asks_for_tool = any(
        phrase in text
        for phrase in [
            "tool",
            "software",
            "recommend",
            "where can i find",
            "how do i find",
            "ad library",
            "spy",
        ]
    )

    if asks_for_tool:
        return (
            "One thing I would look at is which ads have stayed active for several weeks, "
            "not just which ones are new. If a competitor keeps an ad live for a long time, "
            "it can be a useful signal that the angle, offer, or creative is working. I am "
            "building a small tool called RunningAds around that exact workflow, but you can "
            "also do the same manually by comparing long-running ads and repeated messaging patterns."
        )

    return (
        "I would separate new ads from ads that have stayed active for a while. Long-running ads "
        "are not proof that something works, but they are often a better signal than whatever is "
        "new this week. Comparing the hooks, offers, and creative formats across those ads can "
        "give you a stronger starting point for your own tests."
    )


def _score_with_openai(candidates, errors):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or not candidates:
        return candidates

    client = OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4.1-mini"
    compact_posts = [
        {
            "id": item["id"],
            "subreddit": item["subreddit"],
            "title": item["title"],
            "matched_keywords": item["matched_keywords"],
            "rule_score": item["relevance_score"],
        }
        for item in candidates
    ]

    prompt = f"""
You score Reddit posts for RunningAds, a tool that helps marketers find competitor ads that keep running.

Return strict JSON with this shape:
{{"items":[{{"id":"...","relevance_score":0,"explanation":"...","suggested_reply":"..."}}]}}

Scoring rules:
- 0 means unrelated, 10 means very relevant.
- High scores: finding competitor ads, Meta/Facebook Ads Library, winning creatives, ad fatigue, competitor spying, researching ads before campaigns.
- Low scores: memes, hiring, crypto, generic motivation, personal finance, coding unrelated to ads.

Suggested reply rules:
- Helpful first, short, natural, and non-spammy.
- Never pretend to be unaffiliated.
- Mention RunningAds only when naturally relevant.
- Avoid links unless the post clearly asks for tools or resources.
- Do not suggest posting automation, DMs, account creation, or spam.

Posts:
{json.dumps(compact_posts, ensure_ascii=True)}
""".strip()

    try:
        response = client.responses.create(model=model, input=prompt)
        raw_items = json.loads(response.output_text).get("items", [])
        by_id = {str(item.get("id")): item for item in raw_items}
    except Exception as exc:
        errors.append(f"OpenAI scoring unavailable, using rule-based scoring: {str(exc)}")
        return candidates

    improved = []
    for candidate in candidates:
        ai_item = by_id.get(str(candidate["id"]))
        if ai_item:
            candidate = dict(candidate)
            candidate["relevance_score"] = _coerce_score(
                ai_item.get("relevance_score"),
                candidate["relevance_score"],
            )
            candidate["explanation"] = (
                str(ai_item.get("explanation") or candidate["explanation"]).strip()
            )
            candidate["suggested_reply"] = (
                str(ai_item.get("suggested_reply") or candidate["suggested_reply"]).strip()
            )
        improved.append(candidate)
    return improved


def _coerce_score(value, fallback):
    try:
        return max(0, min(10, int(round(float(value)))))
    except (TypeError, ValueError):
        return fallback


def _format_age(created_utc):
    try:
        created = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "unknown"

    delta = datetime.now(timezone.utc) - created
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(1, minutes)}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"
