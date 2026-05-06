import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests


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
            normalized_item = self._normalize_item(item, brand)
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

        return deduped

    def _normalize_item(self, item: Dict[str, Any], brand: str) -> Optional[Dict[str, Any]]:
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

        if not self._could_be_relevant_candidate(page_name, brand):
            return None

        days_running = max(0, (datetime.now(timezone.utc) - start_dt).days)

        if days_running < 7:
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

        snapshot_url = self._first_non_empty(
            item.get("ad_snapshot_url"),
            item.get("snapshotUrl"),
            item.get("snapshot_url"),
            self._build_fallback_snapshot_url(ad_id),
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

    def _could_be_relevant_candidate(self, page_name: str, brand: str) -> bool:
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

            if token == "ridge":
                return page_norm in ("ridge", "the ridge")

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
        images = item.get("images")
        if isinstance(images, list) and images:
            first_image = images[0]
            if isinstance(first_image, str) and first_image.strip():
                return first_image.strip()

        videos = item.get("videos")
        if isinstance(videos, list) and videos:
            first_video = videos[0]
            if isinstance(first_video, str) and first_video.strip():
                return first_video.strip()

        return self._first_non_empty(
            item.get("originalImageUrl"),
            item.get("imageUrl"),
            item.get("image_url"),
            item.get("videoHdUrl"),
            item.get("videoSdUrl"),
            item.get("video_url"),
            "",
        ) or ""

    def _build_fallback_snapshot_url(self, ad_id: Optional[str]) -> str:
        if not ad_id:
            return ""
        return f"https://www.facebook.com/ads/library/?id={ad_id}"

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