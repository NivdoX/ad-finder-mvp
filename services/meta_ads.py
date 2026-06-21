import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests


IMAGE_URL_KEYS = (
    "originalImageUrl",
    "original_image_url",
    "resizedImageUrl",
    "resized_image_url",
    "imageUrl",
    "image_url",
    "previewImageUrl",
    "preview_image_url",
    "thumbnailUrl",
    "thumbnail_url",
    "videoThumbnailUrl",
    "video_thumbnail_url",
    "videoPreviewImageUrl",
    "video_preview_image_url",
    "posterUrl",
    "poster_url",
    "mediaUrl",
    "media_url",
    "image",
    "poster",
    "picture",
    "thumbnail",
)

IMAGE_COLLECTION_KEYS = (
    "images",
    "imageUrls",
    "image_urls",
    "imageAssets",
    "image_assets",
)

NESTED_MEDIA_KEYS = (
    "snapshot",
    "adSnapshot",
    "ad_snapshot",
    "creative",
    "adCreative",
    "ad_creative",
    "cards",
    "carouselCards",
    "carousel_cards",
    "media",
    "asset",
    "assets",
    "videos",
)

NON_IMAGE_EXTENSIONS = (
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".avi",
    ".m3u8",
    ".mp3",
    ".wav",
)

IMAGE_URL_EXPIRY_GRACE_SECONDS = 300


def _facebook_cdn_url_is_expired(parsed) -> bool:
    host = parsed.netloc.lower().split(":", 1)[0]
    if not (host == "fbcdn.net" or host.endswith(".fbcdn.net")):
        return False

    query = {
        key.lower(): value
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    }
    expiry_value = (query.get("oe") or "").strip()
    if not expiry_value:
        return False

    try:
        expires_at = int(expiry_value, 16)
    except ValueError:
        return False

    now = int(datetime.now(timezone.utc).timestamp())
    return expires_at <= now + IMAGE_URL_EXPIRY_GRACE_SECONDS


def normalize_image_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    url = value.strip()
    if not url or any(character.isspace() for character in url):
        return ""

    if url.startswith("//"):
        url = f"https:{url}"

    lowered = url.lower()
    if lowered in {"none", "null", "undefined", "about:blank"}:
        return ""

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""

    path = parsed.path.lower().rstrip("/")
    host = parsed.netloc.lower().split(":", 1)[0]
    if host in {"facebook.com", "www.facebook.com", "m.facebook.com"} and path.startswith("/ads/library"):
        return ""
    if _facebook_cdn_url_is_expired(parsed):
        return ""
    if path.endswith(NON_IMAGE_EXTENSIONS):
        return ""

    query = parsed.query.lower()
    if any(marker in query for marker in ("video/mp4", "video%2fmp4", "application/vnd.apple.mpegurl")):
        return ""

    return url


def is_usable_image_url(value: Any) -> bool:
    return bool(normalize_image_url(value))


def _image_url_from_value(value: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, str):
        return normalize_image_url(value)
    if isinstance(value, list):
        for item in value:
            image_url = _image_url_from_value(item, depth + 1)
            if image_url:
                return image_url
        return ""
    if not isinstance(value, dict):
        return ""

    for key in (*IMAGE_URL_KEYS, "url", "src"):
        image_url = _image_url_from_value(value.get(key), depth + 1)
        if image_url:
            return image_url

    for key in IMAGE_COLLECTION_KEYS:
        image_url = _image_url_from_value(value.get(key), depth + 1)
        if image_url:
            return image_url

    return ""


def _extract_image_from_mapping(item: Dict[str, Any], depth: int = 0) -> str:
    if depth > 6:
        return ""

    for key in IMAGE_URL_KEYS:
        image_url = _image_url_from_value(item.get(key), depth + 1)
        if image_url:
            return image_url

    for key in IMAGE_COLLECTION_KEYS:
        image_url = _image_url_from_value(item.get(key), depth + 1)
        if image_url:
            return image_url

    media_type = str(item.get("type") or item.get("mediaType") or item.get("media_type") or "").lower()
    if "image" in media_type:
        for key in ("url", "src"):
            image_url = _image_url_from_value(item.get(key), depth + 1)
            if image_url:
                return image_url

    for key in NESTED_MEDIA_KEYS:
        nested = item.get(key)
        if isinstance(nested, dict):
            image_url = _extract_image_from_mapping(nested, depth + 1)
            if image_url:
                return image_url
        elif isinstance(nested, list):
            for nested_item in nested:
                if not isinstance(nested_item, dict):
                    continue
                image_url = _extract_image_from_mapping(nested_item, depth + 1)
                if image_url:
                    return image_url

    return ""


def extract_best_image_url(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return _extract_image_from_mapping(item)


class MetaAdsServiceError(Exception):
    pass


class MetaAdsService:
    REQUEST_TIMEOUT_SECONDS = 90

    def __init__(self, apify_token: str, apify_actor_id: str):
        self.apify_token = apify_token
        self.apify_actor_id = apify_actor_id

        self.blocked_page_words = {
            "temu",
            "alibaba",
            "aliexpress",
            "amazon",
            "ebay",
            "wish",
            "etsy",
            "shein",
            "redbubble",
            "spreadshirt",
            "teepublic",
        }

    def search_ads(self, brand: str, country: str, max_results: int = 50) -> List[Dict[str, Any]]:
        return self.search_ads_with_diagnostics(
            brand=brand,
            country=country,
            max_results=max_results,
        )["ads"]

    def search_ads_with_diagnostics(
        self,
        brand: str,
        country: str,
        max_results: int = 50,
        include_young_ads: bool = False,
    ) -> Dict[str, Any]:
        if not self.apify_token:
            raise MetaAdsServiceError("APIFY_TOKEN is missing.")

        if not self.apify_actor_id:
            raise MetaAdsServiceError("APIFY_ACTOR_ID is missing.")

        actor_id = self.apify_actor_id.replace("/", "~")
        url = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"

        headers = {
            "Authorization": f"Bearer {self.apify_token}",
            "Content-Type": "application/json",
        }

        payload = {
            "searchTerms": brand,
            "country": country,
            "activeStatus": "active",
            "maxResults": max_results,
            "enrichAds": True,
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise MetaAdsServiceError(
                "Search timed out. Please try again in a moment. This search was not counted."
            ) from exc
        except requests.RequestException as exc:
            raise MetaAdsServiceError(
                "Could not complete the search right now. Please try again in a moment. This search was not counted."
            ) from exc

        if response.status_code not in (200, 201):
            raise MetaAdsServiceError(
                "Could not complete the search right now. Please try again in a moment. This search was not counted."
            )

        try:
            raw_items = response.json()
        except ValueError as exc:
            raise MetaAdsServiceError(
                "Could not read ad results. Please try again in a moment. This search was not counted."
            ) from exc

        if not isinstance(raw_items, list):
            raise MetaAdsServiceError(
                "Could not read ad results. Please try again in a moment. This search was not counted."
            )

        normalized: List[Dict[str, Any]] = []
        for item in raw_items:
            normalized_item = self._normalize_item(
                item,
                brand,
                include_young_ads=include_young_ads,
            )
            if normalized_item:
                normalized.append(normalized_item)

        normalized.sort(
            key=lambda x: x["days_running"],
            reverse=True,
        )

        seen_ids = set()
        deduped = []
        for ad in normalized:
            ad_id = ad.get("ad_id") or ""
            if ad_id and ad_id in seen_ids:
                continue
            if ad_id:
                seen_ids.add(ad_id)
            deduped.append(ad)

        return {
            "ads": deduped,
            "raw_result_count": len(raw_items),
            "normalized_count": len(deduped),
            "pre_dedupe_count": len(normalized),
            "query": brand,
            "country": country,
            "max_results": max_results,
        }

    def _normalize_item(
        self,
        item: Dict[str, Any],
        brand: str,
        include_young_ads: bool = False,
    ) -> Optional[Dict[str, Any]]:
        is_active = self._extract_is_active(item)
        if is_active is False:
            return None

        start_dt = self._extract_start_datetime(item)
        if not start_dt:
            return None

        page_name = self._clean_text(
            self._first_non_empty(
                item.get("page_name"),
                item.get("pageName"),
                item.get("pageTitle"),
                "",
            )
        )

        if not page_name:
            return None

        if self._is_blocked_page(page_name):
            return None

        landing_page = self._first_non_empty(
            item.get("link_url"),
            item.get("linkUrl"),
            item.get("landing_page"),
            item.get("landingPage"),
            item.get("url"),
        )

        if not self._could_be_relevant_candidate(
            page_name,
            brand,
            landing_page,
        ):
            return None

        days_running = max(0, (datetime.now(timezone.utc) - start_dt).days)

        if days_running < 7 and not include_young_ads:
            return None

        ad_text = self._clean_text(
            self._first_non_empty(
                item.get("ad_text"),
                item.get("adText"),
                item.get("body"),
                item.get("caption"),
                item.get("headline"),
                item.get("title"),
                "",
            )
        )

        headline = self._clean_text(
            self._first_non_empty(
                item.get("headline"),
                item.get("title"),
                item.get("linkTitle"),
                "",
            )
        )

        cta_text = self._clean_text(
            self._first_non_empty(
                item.get("cta_text"),
                item.get("callToAction"),
                item.get("ctaType"),
                "",
            )
        )

        ad_id = self._first_non_empty(
            item.get("ad_id"),
            item.get("adId"),
            item.get("adArchiveID"),
            item.get("adArchiveId"),
        )

        snapshot_url = self._force_english_locale(
        self._first_non_empty(
            item.get("ad_snapshot_url"),
            item.get("snapshotUrl"),
            item.get("snapshot_url"),
            self._build_fallback_snapshot_url(ad_id),
    )
)

        landing_page = self._first_non_empty(
            item.get("landingPageUrl"),
            item.get("landing_page_url"),
            item.get("landingPage"),
            item.get("linkUrl"),
            item.get("url"),
            item.get("finalUrl"),
            "",
        )

        media_url = self._extract_media_url(item)
        start_date_display = start_dt.strftime("%d.%m.%Y")

        return {
            "ad_id": ad_id or "",
            "page_name": page_name,
            "advertiser_name": page_name,
            "ad_text": ad_text,
            "headline": headline,
            "cta_text": cta_text,
            "landing_page": landing_page or "",
            "snapshot_url": snapshot_url or "",
            "media_url": media_url or "",
            "start_date_display": start_date_display,
            "days_running": days_running,
        }

    def _could_be_relevant_candidate(
        self,
        page_name: str,
        brand: str,
        landing_page: str = "",
    ) -> bool:
        page_norm = self._normalize_text(page_name)
        brand_norm = self._normalize_text(brand)

        if not page_norm or not brand_norm:
            return False

        if page_norm == brand_norm:
            return True

        page_tokens = set(page_norm.split())
        brand_tokens = [token for token in brand_norm.split() if token]

        if not brand_tokens:
            return False

        if len(brand_tokens) == 1:
            token = brand_tokens[0]
            landing_raw = (landing_page or "").lower()

            if token in ("ridge", "gymshark", "manscaped"):
                return (
                    page_norm in (token, f"the {token}")
                    or f"://{token}.com" in landing_raw
                    or f"://www.{token}.com" in landing_raw
                )

            return token in page_tokens

        return all(token in page_tokens for token in brand_tokens)

    def _is_blocked_page(self, page_name: str) -> bool:
        page_norm = self._normalize_text(page_name)

        if not page_norm:
            return True

        for blocked_word in self.blocked_page_words:
            blocked_norm = self._normalize_text(blocked_word)
            if blocked_norm and blocked_norm in page_norm:
                return True

        padded = f" {page_norm} "

        hard_noise_patterns = [
            " fan page ",
            " fans page ",
            " fan club ",
            " replica ",
            " dupes ",
            " dupe ",
            " publications ",
        ]

        for pattern in hard_noise_patterns:
            if pattern in padded:
                return True

        return False

    def _extract_is_active(self, item: Dict[str, Any]) -> Optional[bool]:
        for key in ["is_active", "isActive"]:
            value = item.get(key)
            if isinstance(value, bool):
                return value

        end_date = self._first_non_empty(
            item.get("end_date"),
            item.get("endDate"),
            item.get("endDateFormatted"),
        )
        if end_date:
            return False

        return True

    def _extract_start_datetime(self, item: Dict[str, Any]) -> Optional[datetime]:
        timestamp_candidate = self._first_non_empty(
            item.get("startDate"),
            item.get("start_date_timestamp"),
            item.get("start_timestamp"),
        )
        if timestamp_candidate:
            dt = self._parse_timestamp(timestamp_candidate)
            if dt:
                return dt

        string_candidate = self._first_non_empty(
            item.get("start_date"),
            item.get("startDateFormatted"),
            item.get("start_date_formatted"),
        )
        if string_candidate:
            dt = self._parse_date_string(string_candidate)
            if dt:
                return dt

        return None

    def _parse_timestamp(self, value: Any) -> Optional[datetime]:
        try:
            ts = int(value)
            if ts > 9999999999:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def _parse_date_string(self, value: Any) -> Optional[datetime]:
        if not value or not isinstance(value, str):
            return None

        value = value.strip()

        formats = [
            "%b %d, %Y",
            "%B %d, %Y",
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(value, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        try:
            if value.endswith("Z"):
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _extract_media_url(self, item: Dict[str, Any]) -> str:
        return extract_best_image_url(item)

    def _force_english_locale(self, url: str) -> str:
        if not url:
            return ""

        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["locale"] = "en_US"

        return urlunparse(
            parsed._replace(query=urlencode(query))
        )

    def _build_fallback_snapshot_url(self, ad_id: Optional[str]) -> str:
        if not ad_id:
            return ""
        return self._force_english_locale(
            f"https://www.facebook.com/ads/library/?id={ad_id}"
        )

    def _clean_text(self, value: Any) -> str:
        if not value:
            return ""

        text = str(value)
        text = text.replace("\\n", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _normalize_text(self, value: Any) -> str:
        if not value:
            return ""

        text = str(value).lower().strip()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.replace("&", " and ")
        text = re.sub(r"[^a-z0-9]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _first_non_empty(self, *values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None
