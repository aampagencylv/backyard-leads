# Local Trust — Design System

> Brand contract for the utility trades: HVAC, plumbing, electrical,
> roofing, garage doors, water damage restoration, fence repair, pest
> control, septic, locksmith (non-emergency tier).
>
> Confident, no-nonsense, trade-utility. The reader should think
> "these people are licensed and they'll pick up the phone."
> NOT "this looks expensive."

## 1. Color philosophy

Solid blocks of color, not gradients. Primary drives major UI elements
(buttons, trust badges, phone CTA). Paper white background — NOT cream
or ivory (that's luxury). Bold contrast.

- **Paper** (`#ffffff`) — body background. Pure white.
- **Ink** (`#0c1014`) — headings, primary text. Near-black.
- **Ink soft** (`#3d4451`) — body text.
- **Steel** (`#5a6472`) — secondary text, captions.
- **Mist** (`#e6e8eb`) — borders, dividers.
- **Cream-warn** (`#f7f9fc`) — secondary background (NOT warm cream — cool off-white).
- **Primary** — extracted from prospect's logo (typically blue, red, or green for these trades).
- **Primary deep** — auto-darkened.
- **Accent yellow** (`#fbb800`) — used SPARINGLY for "Same-Day Service" / "Licensed & Insured" badges. Only when literal certification labels apply.

## 2. Typography

Default pairing: **Manrope + Manrope** (single-family minimalist), but
the per-prospect DNA may override to Inter+Inter or Archivo+Archivo.

Hierarchy is built on WEIGHT contrast (400/500/700/800), not multiple
typefaces. The "no-nonsense" feel comes from single-family discipline.

- Hero headline: 48-72px, weight 800 (NOT 600 — utility trades want force)
- Section headline: 36-48px, weight 700
- Eyebrow / label: 12px, uppercase, letter-spacing 0.12em, weight 600
- Body: 17px, line-height 1.6 (tighter than luxury's 1.8)
- Trust numbers: 56-80px, weight 800, tabular-nums
- Small text / captions: 14px, weight 500

## 3. Spacing

Compact and efficient. Sections: 80-96px desktop, 56-72px mobile.
NOT 160px luxury-style — utility trades want INFORMATION DENSITY, not breath.

Card padding: 32px. Generous but not lavish.

## 4. Layout

- **Header**: phone number is BIG and CLICKABLE, positioned far-right. License # in small caps under business name.
- **Hero**: split-layout — text on solid-color block (40% width left), photo on right (60% width). NOT full-bleed photo. Text foreground always wins. Tan/dark wood photos common in this vertical — solid color block keeps text readable.
- **Trust strip**: full-width band beneath hero. 4-up stats (years, jobs done, license #, warranty). Big bold numbers.
- **Services**: 3-column grid with thin border + hover-state primary-color border. Each service tile shows: icon-or-number, name, 1-sentence promise.
- **About**: 2-column (text + image), text-first. Photo of the actual team / truck / shop preferred. Workhorse aesthetic.
- **Service Area** (NEW SECTION not in modern_outdoor): bullet list of cities/zips served. Critical for local SEO + sets expectations.
- **Reviews / Testimonials**: 3-up grid (NOT single quote like luxury). Real names + cities. 5-star rating glyphs.
- **CTA section**: full-bleed primary-color block with phone + form CTAs.

## 5. Components

### Buttons
- Primary: SOLID FILL bg primary / text white / padding 14px 32px / border-radius 6px / weight 700.
- Phone CTA: same style but with phone icon prefix.
- Secondary: outlined, primary color, same radius.
- ROUNDED CORNERS OK here (6px) — utility trades like a friendly chunky button.

### Trust badges
- "Licensed & Insured #ABC123" → small pill, bg accent yellow, text ink, weight 700.
- "24 Years Serving Phoenix" → outlined pill, primary border, primary text.
- "BBB A+ Rated" → outlined pill.
- Used in hero + footer + service tiles.

### Phone number treatment
- Header: large monospace-feeling weight 700 with phone icon. ALWAYS tel: linked.
- Hero CTA: big primary button with phone icon.
- Footer: repeated.

### Photos
- Slight rounded corners (8px) on photos — friendly not editorial.
- No filter, no desaturation. Trade photos are usually honest record shots; let them be.
- Hover: subtle box-shadow lift (NOT scale).

### Section dividers
- 1px solid `var(--mist)` between sections. Subtle but present.

## 6. Motion

Minimal. Button hover: 0.15s background-color transition. Photo hover:
0.2s box-shadow rise. NO ken-burns. NO scroll animations. NO carousels.

Utility trade customers are usually in a hurry (broken AC, leaking pipe).
Animation that delays content reading = lost lead.

## 7. Voice / tone

- **Direct, time-aware.** "Most days we can be at your house within 4 hours." Specifics over promises.
- **License number in writing.** "FL HVAC Lic #CAC1817xxx" goes in the hero, footer, anywhere skeptical customers look.
- **Phone-first.** "Call (555) 123-4567" beats "Get a quote" in this vertical.
- **Service area named.** "Serving Tucson, Marana, Oro Valley, and Sahuarita since 1997."
- **Local language, not corporate.** "Your AC is out" beats "Your HVAC system has malfunctioned."
- **Promise vs. claim.** "$95 service call — flat" beats "Affordable rates." Real numbers when possible.
- **Hero headline = the problem they're calling about.** "Pool getting cloudy" / "AC won't cool" / "Pipe burst overnight" — speaks to the moment, not the brand.
- **NO superlatives unless certified.** "BBB A+ rated" (verifiable, OK). "Best in town" (no, don't).

## 8. Brand guidelines

- The homepage is a FUNNEL. Goal: phone call OR form submit within 60 seconds. Every section should make calling easier.
- Footer always shows: license #, business hours, service area, phone, "Designed by {Agency Name}".
- Top-of-page banner: "Preview · Built for {Business Name}" — discreet but present.

## 9. Anti-patterns

DO NOT generate:

- Stock photos of suited consultants
- "Why choose us?" sections with checkmarks (over-used in this vertical — feels lazy)
- Pricing of any kind (specific prices invite haggling; let the phone call set expectations)
- Hero videos
- Chat widgets that delay the call CTA
- Newsletter signups (trade leads don't subscribe)
- Endless service lists (3-6 core services max)
- "We're family-owned" without naming the family
- "Quality work" without naming the warranty terms
- Big "30 years of experience" without naming the YEAR FOUNDED
- Any text the prospect needs to scroll past to find the phone number
- More than ONE hero CTA (Phone OR Form — pick one based on prospect)
- The word "solutions"
- Industry jargon ("HVAC" is OK; "ductless mini-split system optimization" is not)
- Exclamation marks except in trust badges ("Same-Day Service!" is OK in a pill, "Call Now!" is NOT)
