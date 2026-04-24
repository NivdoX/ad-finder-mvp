import json
import re
from urllib.parse import urlparse

from openai import OpenAI


class AdRelevanceFilter:
    def __init__(self, api_key: str = "", model: str = "gpt-4.1-mini"):
        self.api_key = (api_key or "").strip()
        self.model = model
        self.client = OpenAI(api_key=self.api_key) if self.api_key else None

        self.marketplace_domains = {
            "temu.com",
            "amazon.com",
            "amazon.co.uk",
            "amazon.de",
            "amazon.fr",
            "amazon.es",
            "amazon.it",
            "amazon.nl",
            "amazon.se",
            "amazon.no",
            "ebay.com",
            "etsy.com",
            "aliexpress.com",
            "wish.com",
            "redbubble.com",
            "spreadshirt.com",
            "teepublic.com",
        }

        self.marketplace_names = {
            "temu",
            "amazon",
            "ebay",
            "etsy",
            "aliexpress",
            "wish",
            "redbubble",
            "spreadshirt",
            "teepublic",
        }

    def filter_ads(self, search_brand: str, ads: list[dict]) -> list[dict]:
        if not ads:
            return ads

        filtered_ads = []

        for ad in ads:
            try:
                if self._is_relevant(search_brand=search_brand, ad=ad):
                    filtered_ads.append(ad)
            except Exception as exc:
                print("Ad relevance filter error:", str(exc))
                # Fail safe: behold annonsen hvis noe går skikkelig galt på én ad
                filtered_ads.append(ad)

        # Fail safe: hvis alt blir filtrert bort pga AI eller parsing-feil,
        # fall tilbake til enkel regelbasert filtrering, og hvis det også blir tomt,
        # returner originale ads.
        if filtered_ads:
            return filtered_ads

        fallback_ads = []
        for ad in ads:
            try:
                if self._rule_based_relevant(search_brand=search_brand, ad=ad):
                    fallback_ads.append(ad)
            except Exception:
                pass

        return fallback_ads if fallback_ads else ads

    def _is_relevant(self, search_brand: str, ad: dict) -> bool:
        # 1. Først raske regler
        rule_result = self._rule_based_decision(search_brand=search_brand, ad=ad)
        if rule_result is not None:
            return rule_result

        # 2. Så AI, hvis tilgjengelig
        if self.client:
            ai_result = self._ai_decision(search_brand=search_brand, ad=ad)
            if ai_result is not None:
                return ai_result

        # 3. Fallback
        return self._rule_based_relevant(search_brand=search_brand, ad=ad)

    def _rule_based_decision(self, search_brand: str, ad: dict) -> bool | None:
        brand = self._normalize(search_brand)

        advertiser = self._normalize(self._pick(ad, "advertiser_name", "page_name"))
        headline = self._normalize(self._pick(ad, "headline", "title"))
        ad_text = self._normalize(self._pick(ad, "ad_text", "text", "body"))
        cta_text = self._normalize(self._pick(ad, "cta_text", "call_to_action"))
        landing_page = self._pick(ad, "landing_page", "landing_page_url", "url", "final_url", "snapshot_url")
        domain = self._extract_domain(landing_page)

        combined = " ".join([advertiser, headline, ad_text, cta_text, domain]).strip()

        # Klar irrelevans: marketplace / merch / random seller
        if any(name in advertiser for name in self.marketplace_names):
            return False

        if any(name in domain for name in self.marketplace_names):
            return False

        if self._looks_like_official_brand_match(brand, advertiser, domain):
            return True

        # Hvis brandet ikke nevnes noe sted, er det nesten alltid NO
        if not self._brand_mentioned(brand, combined):
            return False

        # Hvis brand nevnes, men bare i random marketplace/ad seller kontekst
        if any(name in combined for name in self.marketplace_names):
            return False

        # Ikke nok sikkerhet -> send til AI
        return None

    def _rule_based_relevant(self, search_brand: str, ad: dict) -> bool:
        brand = self._normalize(search_brand)

        advertiser = self._normalize(self._pick(ad, "advertiser_name", "page_name"))
        headline = self._normalize(self._pick(ad, "headline", "title"))
        ad_text = self._normalize(self._pick(ad, "ad_text", "text", "body"))
        cta_text = self._normalize(self._pick(ad, "cta_text", "call_to_action"))
        landing_page = self._pick(ad, "landing_page", "landing_page_url", "url", "final_url", "snapshot_url")
        domain = self._extract_domain(landing_page)

        combined = " ".join([advertiser, headline, ad_text, cta_text, domain]).strip()

        if self._looks_like_official_brand_match(brand, advertiser, domain):
            return True

        if not self._brand_mentioned(brand, combined):
            return False

        if any(name in combined for name in self.marketplace_names):
            return False

        # Tvil = NO
        return False

    def _ai_decision(self, search_brand: str, ad: dict) -> bool | None:
        advertiser = self._pick(ad, "advertiser_name", "page_name")
        headline = self._pick(ad, "headline", "title")
        ad_text = self._pick(ad, "ad_text", "text", "body")
        cta_text = self._pick(ad, "cta_text", "call_to_action")
        landing_page = self._pick(ad, "landing_page", "landing_page_url", "url", "final_url", "snapshot_url")

        prompt = f"""
You are filtering ads for relevance.
The user searched for this brand: {search_brand}

Your job is to decide if this ad is truly relevant to that brand.

Relevant means:
The ad is from the official brand, or
The ad clearly promotes that brand’s actual products or services, or
It is a legitimate collaboration where the searched brand is a central part of the ad.

Not relevant means:
The ad only mentions the brand casually,
The ad is selling unofficial merch, copies, or unrelated marketplace products,
The ad is from a random seller or aggregator with no clear official link,
The ad is not clearly about the searched brand.

If you are unsure, answer NO.

Return JSON only in this format:
{{ "relevant": true }}
or
{{ "relevant": false }}

Ad data:
Advertiser: {advertiser or ""}
Headline: {headline or ""}
Text: {ad_text or ""}
CTA: {cta_text or ""}
Landing page: {landing_page or ""}
""".strip()

        try:
            response = self.client.responses.create(
                model=self.model,
                input=prompt,
            )
            raw = (response.output_text or "").strip()
            parsed = json.loads(raw)
            return bool(parsed.get("relevant") is True)
        except Exception as exc:
            print("AI relevance decision failed:", str(exc))
            return None

    def _pick(self, ad: dict, *keys: str) -> str:
        for key in keys:
            value = ad.get(key)
            if value:
                return str(value)
        return ""

    def _normalize(self, value: str) -> str:
        value = (value or "").lower().strip()
        value = re.sub(r"[^a-z0-9\s]", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def _extract_domain(self, url: str) -> str:
        if not url:
            return ""

        try:
            parsed = urlparse(url if "://" in url else f"https://{url}")
            host = (parsed.netloc or "").lower().strip()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    def _brand_mentioned(self, brand: str, text: str) -> bool:
        if not brand or not text:
            return False

        if brand in text:
            return True

        brand_tokens = [token for token in brand.split() if token]
        if not brand_tokens:
            return False

        matches = sum(1 for token in brand_tokens if token in text)
        return matches >= max(1, len(brand_tokens))

    def _looks_like_official_brand_match(self, brand: str, advertiser: str, domain: str) -> bool:
        if not brand:
            return False

        brand_tokens = [token for token in brand.split() if token]
        if not brand_tokens:
            return False

        advertiser_match = brand in advertiser or all(token in advertiser for token in brand_tokens)
        domain_match = any(token in domain for token in brand_tokens)

        # sterkeste signal: advertiser matcher brand + domain matcher brand
        if advertiser_match and domain_match:
            return True

        # også lov hvis advertiser er veldig tydelig brandet, selv uten domain
        if advertiser_match and len(brand_tokens) == 1:
            return True

        return False