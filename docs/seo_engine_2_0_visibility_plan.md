# RunningAds SEO Engine 2.0 visibility plan

RunningAds already has DB-backed SEO brand pages, candidate generation, candidate testing, quality scoring, promotion, cached ad previews, durable image preservation, and admin audit views. SEO Engine 2.0 should turn that manual engine into a controlled visibility system without mass publishing weak pages.

## Operating principle

The engine should maintain existing published pages before creating more pages.

Priority order:

1. Repair published pages missing images.
2. Repair stale, failed, or empty cache.
3. Preserve external ad images durably.
4. Promote qualified candidates only after quality gates pass.
5. Generate or discover more candidates.

Public SEO pages must never trigger Apify, OpenAI, scraping, cron loops, or paid refresh work. Public pages should read saved brand metadata, cached preview ads, and durable image records only.

## Current SEO pipeline

Discover -> Qualify -> Promote -> Build -> Validate -> Publish -> Maintain

- Discover: `/admin/seo-candidate-generator`
- Qualify: `/admin/seo-brand-candidates`
- Promote: candidate promote and bulk promote actions
- Build: `/brand/<brand_slug>` from `seo_brands`
- Validate: `/admin/seo-engine-2` and `/admin/seo-market-audit`
- Publish: published DB rows plus `/sitemap.xml`
- Maintain: `/admin/seo-brand-cache`

## Quality gates

A page should not be considered SEO ready unless it has:

- Brand name
- Clean slug
- Commercial category and focus
- Existing ad previews or strong preview potential
- Useful template text from metadata
- Usable ad images or durable image candidates
- No critical cache failure with no fallback previews
- Clear CTA back to search or account creation
- Internal links to related public pages

A page must not be auto-published if it has:

- No useful ad previews
- No image signal
- Generic or empty page text
- Broken cache with no saved fallback
- Low commercial relevance
- Duplicate or unclear brand entity
- Unsupported or irrelevant brand type

## Brand page upgrades

Brand pages should continue using one scalable template, but the data model should keep enough metadata for pages to feel specific:

- Focus
- Audience
- Creative angle
- Market context
- Category
- Summary
- FAQ candidates
- Comparison targets
- Related brands
- Last useful ad snapshot
- Durable image status

For 500+ pages, the metadata should live in the database and be editable from admin. `seo_brands.py` should remain only as a legacy fallback until the static list is no longer needed.

## Internal linking strategy

Keep current related-brand links on brand pages. Add higher-level pages later so crawlers and visitors can understand the product as an entity, not only as a list of brand URLs.

Recommended public hub structure:

- Home
  - About RunningAds
  - Why we built RunningAds
  - Brand research examples
  - Pricing
  - Changelog / product updates
  - Transparent roadmap
  - Brand category hubs
    - Brand pages
  - Founder / company authority
    - About NivDoX AI
    - Founder page

Brand pages should link back to the most relevant category hub when those hubs exist. Category hubs should link to the strongest published brands first, not every brand at once.

## Founder and brand authority roadmap

These pages should be practical, transparent, and calm. They should explain what RunningAds is, who built it, what it does not promise, and how it fits into real ad research workflows. Avoid fake guru language, inflated claims, or any promise that RunningAds guarantees winning ads.

### 1. About RunningAds page

Purpose: explain RunningAds as a tool for finding and comparing active ads, long-running creatives, and saved ad examples.

Internal links:

- Home navigation or footer
- Pricing
- Sample results
- Brand category hubs
- Why we built RunningAds

### 2. About NivDoX AI page

Purpose: make the company/entity behind RunningAds clear to Google, AI systems, and visitors.

Internal links:

- Footer
- About RunningAds
- Founder page
- Transparent roadmap

### 3. Founder page

Purpose: show a real person behind the product, with practical background and product philosophy.

Internal links:

- About NivDoX AI
- Why we built RunningAds
- Changelog / product updates
- Transparent roadmap

### 4. Why we built RunningAds page

Purpose: explain the problem plainly: ad research is scattered, live ad libraries are noisy, and marketers need a faster way to compare active ad examples.

Internal links:

- About RunningAds
- Founder page
- Real ad research workflow examples
- Sample results

### 5. Changelog / product updates page

Purpose: show that the product is maintained. This helps trust, freshness, and entity understanding.

Internal links:

- About RunningAds
- Transparent roadmap
- SEO brand cache improvements or sample result examples where relevant

### 6. Transparent roadmap page

Purpose: state what is being improved without overpromising. Include practical items like better image preservation, more qualified brand pages, and cleaner workflows.

Internal links:

- Changelog / product updates
- About RunningAds
- Founder page

### 7. Real ad research workflow examples

Purpose: show how to use RunningAds in realistic workflows without claiming that any ad is guaranteed to win.

Example workflows:

- Compare three competitors before writing a new Meta ad brief.
- Review long-running ads before refreshing an offer.
- Check category patterns before making a landing page.
- Save examples for a creative swipe file.

Internal links:

- Home
- Sample results
- Brand pages used in examples
- Pricing
- Create account CTA

## Structured data roadmap

Already useful:

- WebPage on brand pages
- BreadcrumbList on brand pages

Next candidates:

- Organization on About RunningAds / About NivDoX AI
- Person on Founder page
- WebPage on authority pages
- FAQPage only when the page has real visible FAQ content
- BreadcrumbList on category hubs and authority pages

Do not add structured data that is not visible or supported by page content.

## Comparison pages

Comparison pages can be useful later, but should not be generated blindly.

Examples:

- RunningAds vs Meta Ads Library workflow
- Brand A ads vs Brand B ads within a category
- Ecommerce ad research workflow
- SaaS ad research workflow

Quality requirements:

- Clear editorial purpose
- Real workflow value
- Internal links to relevant brand pages
- No claims that one tool or ad guarantees performance

## FAQ blocks

FAQ blocks should be generated from real search intent and visible on the page. They should explain practical things:

- What can I learn from active ads?
- Why do long-running ads matter?
- Why might a brand have no cached previews?
- Are these ads guaranteed to be profitable?

Answer the last question clearly: no. RunningAds shows signals and examples, not guaranteed winners.

## Free tools and tentpole assets

Future assets that fit the product:

- Ad research checklist
- Competitor ad brief template
- Creative angle worksheet
- Meta ad swipe file structure
- DTC ad research examples
- SaaS ad research examples

These should link to brand pages and sample results.

## YouTube search mapping

RunningAds can later publish practical videos or pages around:

- How to research competitor ads
- How to compare long-running ads
- How to build a swipe file
- How to use Meta Ads Library with RunningAds

Videos should link back to relevant workflow pages, not just the homepage.

## External mentions

Future distribution can include:

- SaaS directories
- Startup directories
- Product update communities
- Relevant Reddit discussions when genuinely helpful
- Founder/product build notes
- Practical examples shared on LinkedIn or YouTube

Avoid spammy directory blasts or generic AI-written posts.

## AI visibility tracker

Add an internal tracker later for:

- Queries where RunningAds should be mentioned
- Whether Google AI Overview / AI Mode mentions the product
- Whether ChatGPT, Perplexity, or Gemini understand what RunningAds does
- Which pages support each entity claim
- External citations that mention RunningAds or NivDoX AI

## Phased rollout

### Phase 1: Foundation

- SEO Engine 2.0 admin overview
- Read-only dry-run planner
- Quality gates
- Visibility roadmap
- Founder/authority pages planned in internal linking structure

### Phase 2: Trust and entity pages

- About RunningAds
- About NivDoX AI
- Founder page
- Why we built RunningAds
- Changelog / product updates
- Transparent roadmap
- Real ad research workflow examples

### Phase 3: Category and workflow expansion

- Category hubs
- Workflow pages
- FAQ blocks where useful
- Stronger internal linking from authority pages to brand/category pages

### Phase 4: Controlled autopilot

- Use the admin-triggered maintenance executor as the first safe execution layer for published-page cache and image repair.
- Add a CLI entry point such as `python app.py run-seo-engine-autopilot` or an equivalent command that reuses the same dry-run planner and safety gates.
- CLI dry run
- Admin approval queue
- Bounded autopilot maintenance
- Publish only when quality gates pass
- Kill switch and daily paid-API limits

### Phase 5: AI visibility

- AI visibility tracker
- External mention tracker
- YouTube/search mapping
- Comparison pages and tentpole assets
