# Home Contractor Premium — Design System

> Brand contract for polished multi-service home contractors:
> renovation contractors, multi-trade home-service companies (plumbing
> + HVAC + electrical + solar under one roof), upscale general
> contractors, premium remodelers, multi-vertical home-improvement.
>
> Inspired by absolutehomeservices.ca + carterservices.com. Sits
> between local_trust (utility trade) and luxury (boutique editorial).
> Confidently professional. Booking-first CTAs. Multi-service grid.

## 1. Color philosophy

Editorial-but-trade. Cooler than modern_outdoor's warm cream-and-green.
Anchored on the prospect's actual brand color, with deep navy as fallback.

- **Paper** (`#ffffff`) — body background. Clean white.
- **Cream-cool** (`#f5f7fa`) — section background, panel fill (cooler than wellness cream).
- **Ink** (`#0d1419`) — headings + primary text. Deep cool near-black.
- **Ink soft** (`#3a4452`) — body text.
- **Steel** (`#6b7280`) — secondary text, captions.
- **Mist** (`#e2e8f0`) — borders, dividers.
- **Primary** — extracted from logo. Defaults to deep navy `#1e3a5f` or refined purple `#50267c` (Absolute's signature).
- **Primary deep** — auto-darkened for hover.
- **Accent green** (`#5fa030`) — used for trust badges + "Licensed" pills.

The palette feels "your contractor who shows up in a labeled truck and
doesn't track mud."

## 2. Typography

Default pairing: **Manrope + Manrope** (single-family minimalist), but
the per-prospect DNA may override to spacegrotesk_inter or dmsans_dmserif.

Confident sans hierarchy on heavy weights:

- Hero headline: 56-80px sans, weight 800
- Section headline: 36-52px, weight 700
- Eyebrow / label: 12px, uppercase, letter-spacing 0.14em, weight 700, color = primary
- Body: 17px, line-height 1.65
- Tenure number (in hero / trust strip): 64-88px, weight 800, tabular-nums
- Service category title: 18-20px, weight 700
- Small text: 14px, weight 500

## 3. Spacing

Moderate. Section vertical: 112-128px desktop, 72-88px mobile.
Tighter than luxury, looser than local_trust's information-density approach.

## 4. Layout

- **Top utility bar**: small dark band ABOVE the header. Single line with hours, service area, primary phone. NOT alarm-aware like emergency_service — just useful at-a-glance.
- **Header**: brand block left, service-category nav center (optional), bold primary "Schedule Service" button right + phone secondary.
- **Hero**: split layout — text LEFT on cream-cool block (50%), photo RIGHT (50%). Text foreground wins. Two prominent CTAs: "Schedule Service" (filled primary, primary CTA) + "Get a Free Estimate" (outlined, secondary). Phone link beneath in small caps.
- **Tenure trust strip**: full-width band beneath hero. Big serif tenure number on the left ("25 YEARS"), 3-4 trust pills on the right (Licensed/Insured/BBB/Years). Smaller than local_trust's 4-stat grid — this is about TENURE, not breadth of stats.
- **Multi-service category grid**: 4-column on desktop (3 on tablet, 2 on mobile). Each category card = small icon-or-number badge + service name + 1-sentence promise + "Learn more →" link. Cards stack ALL the prospect's services so the visitor sees the breadth.
- **About / Why call us**: 2-column. Text left (story + credentials), team photo right.
- **Process** (3-step or 4-step): horizontal numbered band. "1. You call → 2. We visit → 3. We quote → 4. We do the work" — sets expectations.
- **Service area + cities**: pill-style city list. Same as local_trust.
- **Reviews**: 3-up grid with 5-star glyphs.
- **CTA section**: full-bleed primary-color block. Schedule CTA + phone secondary.

## 5. Components

### Buttons
- Primary: SOLID FILL primary / text paper / weight 700 / padding 14px 32px / border-radius 8px.
- Secondary "Get Free Estimate": OUTLINED, same shape.
- Phone link: small caps text link with phone icon, NOT a giant button (that's emergency_service / local_trust). Phone is supporting, not primary.
- ROUNDED CORNERS (8px) — friendly polished.

### Tenure number treatment
- Big sans 64-88px serif tenure number with small caps "YEARS" or "YEARS SERVING [CITY]" underneath.
- Used in hero tenure strip + about section.
- Replaces the modern_outdoor "5.0 stars" trust treatment which doesn't have the same gravity.

### Trust pills
- "Licensed & Insured #ABC123" → bg cream-cool, border 1px primary, text primary.
- Used in hero tenure strip + about section.
- More restrained than local_trust's bright yellow alert pills.

### Service category card
- Small icon (or numbered badge) + service name + 1-sentence promise + "Learn more →" arrow link.
- Hover: card border shifts to primary + subtle box-shadow rise.
- 4-column grid on desktop, accommodates up to 12 service categories.

## 6. Motion

Subtle. 0.2s ease on hover states. NO ken-burns. NO scroll animations.

## 7. Voice / tone

- **Schedule-first, not call-first.** "Book a free estimate" beats "Call now". Phone is supporting.
- **Tenure forward.** "Serving GTA since 1998" / "25 years in San Diego" — leading with years, not awards.
- **Specific service categories.** Show the breadth: "Plumbing · HVAC · Electrical · Solar · Appliance Repair" — visitor sees their problem covered.
- **Confident, not hard-sell.** "We do quality work and we show up when we say we will." NOT "BEST IN TOWN!"
- **Licensed + insured stated plainly.** "FL HVAC Lic #CAC1817xxx · BBB A+" — facts, in the hero.
- **Hero headline = the promise the prospect wants from a polished contractor.** Examples:
  - "Home repairs done right the first time, on schedule."
  - "Your home, in capable hands since 1998."
  - "One contractor. Every system. No subcontractor chaos."

## 8. Brand guidelines

- The homepage is a CREDIBILITY FUNNEL. Goal: schedule a free estimate OR call within 60 seconds. The visitor is comparing contractors; we win by looking professional.
- Footer always shows: license #, business hours, service area, primary phone, "Designed by {Agency Name}".
- Top utility bar persistent on scroll.

## 9. Anti-patterns

DO NOT generate:

- Cheap-looking trust badges (graphic chevrons, generic ribbons)
- Stock photos of suited men shaking hands
- Pricing (let the free estimate set expectations)
- "We've revolutionized home services!" — no revolution language
- Hero videos or carousels
- Newsletter signups
- Generic "Quality work" claims without years/jobs backing
- Award logos we can't verify
- "Why choose us?" sections with checkmarks (use the multi-service grid instead — it shows breadth without bullets)
- More than ONE primary CTA (Schedule Service) — secondary OK (Get Estimate or Phone)
- The word "solutions"
- "Industry-leading" or "premier"
- Exclamation marks (except trust pills like "Same-Day Service!")
- Big alarm-color blocks (that's emergency_service territory)
