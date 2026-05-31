import re


BRAND_DEFINITIONS = [
    {
        "name": "Gymshark",
        "category": "fitness apparel",
        "focus": "fitness apparel and ecommerce growth",
        "audience": "gym-focused shoppers, creators, and performance marketers",
        "creative_angle": "influencer and UGC-style creatives tied to training culture",
        "market_context": "community-driven marketing where social proof and product launches can move quickly",
    },
    {
        "name": "Nike",
        "category": "sportswear",
        "focus": "sportswear, DTC launches, and performance marketing",
        "audience": "athletes, sneaker buyers, fitness shoppers, and brand marketers",
        "creative_angle": "athlete-driven creatives, product drops, and performance-led storytelling",
        "market_context": "a global sportswear market where brand equity, direct sales, and campaign timing all matter",
    },
    {
        "name": "Manscaped",
        "category": "men's grooming",
        "focus": "men's grooming, subscriptions, and direct response",
        "audience": "men's grooming buyers and conversion-focused ecommerce teams",
        "creative_angle": "humorous hooks, offer-led messages, and conversion-focused creatives",
        "market_context": "a competitive grooming category where memorable angles and repeat-purchase offers are important",
    },
    {
        "name": "Huel",
        "category": "nutrition",
        "focus": "meal replacement, nutrition, and ecommerce subscriptions",
        "audience": "busy professionals, fitness shoppers, and health-conscious buyers",
        "creative_angle": "benefit-led creatives around convenience, nutrition, and routines",
        "market_context": "a nutrition market where trust, habit formation, and subscription messaging matter",
    },
    {
        "name": "AG1",
        "category": "nutrition",
        "focus": "daily supplements, wellness routines, and subscription marketing",
        "audience": "wellness buyers, athletes, creators, and health-conscious professionals",
        "creative_angle": "routine-based creatives, testimonials, and simple daily-use hooks",
        "market_context": "a crowded supplement market where credibility and retention messaging are key",
    },
    {
        "name": "Ridge",
        "category": "accessories",
        "focus": "wallets, accessories, gifting, and ecommerce offers",
        "audience": "minimalist accessory buyers and direct-response ecommerce teams",
        "creative_angle": "problem-solution creatives, product demos, and gifting angles",
        "market_context": "an accessories market where utility, design, and offer clarity can drive purchases",
    },
    {
        "name": "Shopify",
        "category": "ecommerce software",
        "focus": "SaaS, ecommerce merchants, and platform marketing",
        "audience": "founders, ecommerce operators, agencies, and growing merchants",
        "creative_angle": "lead generation, product education, and merchant success stories",
        "market_context": "a platform market where education, trust, and business outcomes shape demand",
    },
    {
        "name": "HubSpot",
        "category": "marketing software",
        "focus": "CRM, marketing automation, and B2B lead generation",
        "audience": "sales teams, marketers, founders, and revenue operators",
        "creative_angle": "educational offers, product demos, and pain-point-led B2B creatives",
        "market_context": "a software market where content, trust, and funnel education support buying decisions",
    },
    {
        "name": "ClickFunnels",
        "category": "marketing software",
        "focus": "funnels, digital products, and conversion marketing",
        "audience": "online sellers, coaches, agencies, and direct-response marketers",
        "creative_angle": "webinar, challenge, and offer-led creatives built around conversion outcomes",
        "market_context": "a direct-response software market where proof, urgency, and education often work together",
    },
    {
        "name": "Monday",
        "category": "work management software",
        "focus": "work management, team productivity, and SaaS acquisition",
        "audience": "operations teams, project managers, and business leaders",
        "creative_angle": "workflow pain points, product education, and team productivity messages",
        "market_context": "a competitive SaaS category where clear use cases help buyers understand fit",
    },
    {
        "name": "Adidas",
        "category": "sportswear",
        "focus": "sportswear, footwear launches, and lifestyle performance marketing",
        "audience": "athletes, sneaker buyers, streetwear shoppers, and sports fans",
        "creative_angle": "product drops, athlete partnerships, and lifestyle-led creatives",
        "market_context": "a global sportswear market where campaign timing and product storytelling are central",
    },
    {
        "name": "On Running",
        "category": "running shoes",
        "focus": "running shoes, performance apparel, and premium ecommerce",
        "audience": "runners, active professionals, and premium footwear shoppers",
        "creative_angle": "performance benefits, product technology, and clean lifestyle creatives",
        "market_context": "a fast-growing running category where credibility and product differentiation matter",
    },
    {
        "name": "Lululemon",
        "category": "activewear",
        "focus": "activewear, lifestyle apparel, and premium retail",
        "audience": "fitness shoppers, yoga communities, and premium apparel buyers",
        "creative_angle": "lifestyle-led creatives, product versatility, and community positioning",
        "market_context": "a premium activewear market where brand affinity and product quality drive demand",
    },
    {
        "name": "Vuori",
        "category": "activewear",
        "focus": "activewear, athleisure, and premium ecommerce",
        "audience": "fitness, travel, and lifestyle shoppers looking for versatile apparel",
        "creative_angle": "comfort-led creatives, lifestyle imagery, and everyday performance hooks",
        "market_context": "an activewear market where comfort, design, and brand identity are strong differentiators",
    },
    {
        "name": "Goli",
        "category": "nutrition",
        "focus": "supplements, wellness gummies, and direct-to-consumer nutrition",
        "audience": "wellness shoppers and ecommerce teams studying supplement offers",
        "creative_angle": "benefit-led hooks, simple product claims, and routine-based creatives",
        "market_context": "a supplement market where format, flavor, and credibility can influence conversion",
    },
    {
        "name": "Athletic Greens",
        "category": "nutrition",
        "focus": "daily nutrition, greens supplements, and subscription marketing",
        "audience": "health-conscious buyers, athletes, and wellness creators",
        "creative_angle": "daily routine messaging, testimonials, and simplified wellness hooks",
        "market_context": "a premium supplement category where trust and habit-building are important",
    },
    {
        "name": "Dollar Shave Club",
        "category": "men's grooming",
        "focus": "razors, grooming subscriptions, and direct-response ecommerce",
        "audience": "men's grooming buyers and subscription marketers",
        "creative_angle": "value-led creatives, humor, and simple replenishment offers",
        "market_context": "a grooming market where convenience and recurring value can be strong hooks",
    },
    {
        "name": "Dr. Squatch",
        "category": "men's grooming",
        "focus": "men's soap, personal care, and ecommerce bundles",
        "audience": "men's grooming shoppers and DTC brand marketers",
        "creative_angle": "humorous hooks, product demos, and bundle-led creatives",
        "market_context": "a personal care category where brand voice and memorable product positioning matter",
    },
    {
        "name": "Casper",
        "category": "home goods",
        "focus": "mattresses, sleep products, and considered-purchase ecommerce",
        "audience": "home shoppers and marketers studying high-consideration DTC offers",
        "creative_angle": "comfort claims, reviews, guarantees, and offer-led creatives",
        "market_context": "a home goods market where trust, proof, and purchase confidence are crucial",
    },
    {
        "name": "Warby Parker",
        "category": "eyewear",
        "focus": "eyewear, home try-on, and omnichannel retail",
        "audience": "style-conscious eyewear shoppers and retail marketers",
        "creative_angle": "style-led creatives, convenience messages, and try-on offers",
        "market_context": "an eyewear market where fit, taste, and convenience help reduce buying friction",
    },
    {
        "name": "Allbirds",
        "category": "footwear",
        "focus": "sustainable footwear, comfort, and lifestyle ecommerce",
        "audience": "comfort-focused shoppers and sustainability-minded buyers",
        "creative_angle": "material stories, comfort claims, and simple lifestyle creatives",
        "market_context": "a footwear market where differentiation often comes from materials, comfort, and values",
    },
    {
        "name": "Peloton",
        "category": "fitness",
        "focus": "connected fitness, memberships, and home workouts",
        "audience": "fitness buyers, busy professionals, and subscription marketers",
        "creative_angle": "motivation-led creatives, instructor/community stories, and product education",
        "market_context": "a fitness market where habit, community, and recurring engagement support growth",
    },
    {
        "name": "MyProtein",
        "category": "nutrition",
        "focus": "sports nutrition, protein products, and ecommerce promotions",
        "audience": "fitness shoppers, athletes, and value-focused supplement buyers",
        "creative_angle": "discount-led creatives, product variety, and performance nutrition hooks",
        "market_context": "a competitive supplement market where offers and product breadth can drive volume",
    },
    {
        "name": "Sephora",
        "category": "beauty",
        "focus": "beauty retail, product discovery, and omnichannel campaigns",
        "audience": "beauty shoppers, makeup enthusiasts, and retail marketers",
        "creative_angle": "product launches, seasonal edits, and discovery-led creatives",
        "market_context": "a beauty market where trends, selection, and loyalty can shape purchase behavior",
    },
    {
        "name": "Glossier",
        "category": "beauty",
        "focus": "beauty, skincare, and community-led ecommerce",
        "audience": "beauty shoppers, skincare buyers, and DTC brand marketers",
        "creative_angle": "minimalist product stories, UGC-style creatives, and community language",
        "market_context": "a beauty market where identity, simplicity, and social proof influence demand",
    },
    {
        "name": "Curology",
        "category": "skincare",
        "focus": "personalized skincare, subscriptions, and direct response",
        "audience": "skincare shoppers looking for acne or routine support",
        "creative_angle": "before-and-after proof, personalization, and consultation-led creatives",
        "market_context": "a skincare market where trust, outcomes, and expert framing matter",
    },
    {
        "name": "Ritual",
        "category": "nutrition",
        "focus": "vitamins, supplements, and subscription wellness",
        "audience": "health-conscious buyers looking for transparent supplement routines",
        "creative_angle": "ingredient transparency, routine messaging, and trust-building creatives",
        "market_context": "a wellness market where proof, transparency, and repeat usage support conversion",
    },
    {
        "name": "Native",
        "category": "personal care",
        "focus": "deodorant, personal care, and scent-led ecommerce",
        "audience": "personal care shoppers and DTC marketers studying repeat-purchase products",
        "creative_angle": "scent stories, clean-ingredient claims, and bundle offers",
        "market_context": "a personal care market where habit, scent, and ingredient positioning can stand out",
    },
    {
        "name": "HelloFresh",
        "category": "meal kits",
        "focus": "meal kits, subscriptions, and household acquisition",
        "audience": "busy households, families, and subscription growth teams",
        "creative_angle": "offer-led creatives, convenience hooks, and weekly meal variety",
        "market_context": "a meal-kit market where discounts, habit-building, and convenience are core messages",
    },
    {
        "name": "Factor",
        "category": "meal kits",
        "focus": "prepared meals, convenience, and subscription ecommerce",
        "audience": "busy professionals, fitness shoppers, and health-focused households",
        "creative_angle": "ready-made meal benefits, offer-led hooks, and routine-based creatives",
        "market_context": "a prepared-meal market where convenience, nutrition, and retention messaging matter",
    },
]


def slugify_brand(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug


def _build_brand_pages():
    pages = []
    for item in BRAND_DEFINITIONS:
        name = item["name"]
        category = item["category"]
        focus = item["focus"]
        audience = item["audience"]
        creative_angle = item["creative_angle"]
        market_context = item["market_context"]

        pages.append(
            {
                **item,
                "slug": slugify_brand(name),
                "search_query": name,
                "headline": f"Find long-running {name} ads",
                "meta_title": f"{name} Ads | Find Long-Running Ads | RunningAds",
                "meta_description": (
                    f"Research active {name} ads across {focus}. "
                    f"See creative patterns for {audience} with RunningAds."
                ),
                "summary": (
                    f"Use RunningAds to research active {name} ads across {focus}. "
                    f"Look for {creative_angle}. Market context: {market_context}."
                ),
            }
        )
    return pages


BRAND_PAGES = _build_brand_pages()
BRAND_PAGE_BY_SLUG = {brand["slug"]: brand for brand in BRAND_PAGES}


def get_brand_by_slug(slug: str):
    return BRAND_PAGE_BY_SLUG.get((slug or "").strip().lower())


def get_related_brands(brand, limit: int = 5):
    if not brand:
        return []

    related = [
        item
        for item in BRAND_PAGES
        if item["slug"] != brand["slug"] and item["category"] == brand["category"]
    ]

    if len(related) < limit:
        related.extend(
            item
            for item in BRAND_PAGES
            if item["slug"] != brand["slug"] and item not in related
        )

    return related[:limit]
