# Luxury — Design System

> Brand contract for high-end personal-service businesses: med spas,
> aesthetic clinics, plastic surgery, fine jewelry, premium salons,
> luxury real estate, classic luxury hospitality.
>
> Editorial. Restrained. Confident without shouting. The reader should
> feel like they wandered into a magazine, not landed on a marketing page.

## 1. Color philosophy

The PROSPECT'S brand color drives the primary slot (extracted from their
logo). The supporting palette stays restrained:

- **Paper / ivory** (`#fdfcf8`) — body background. Warm white, never sterile.
- **Cream** (`#f5f0e7`) — section background, panel fill.
- **Ink** (`#1a1614`) — headings, primary text. Warm near-black with brown undertone, never pure black.
- **Ink soft** (`#5a544e`) — body text. Brand-aware undertone.
- **Mist** (`#e8e3d8`) — hairline borders, dividers (the only divider we use is 1px hairline).
- **Primary** — extracted from prospect's logo.
- **Primary deep** — auto-darkened for hover states.

No accent gold or bright color. The brand's own color is the single
accent — everything else is neutral.

## 2. Typography

Default pairing: **Cormorant Garamond + Inter**, but the per-prospect
DNA may override (e.g. Playfair + Lato for established-traditional).

Hierarchy is built on SCALE + WEIGHT contrast, not multiple typefaces:

- Hero headline: 64-96px serif, weight 500 (NOT 600 — luxury uses lighter weights)
- Section headline: 40-56px serif, weight 500
- Eyebrow / label: 11px sans-serif, uppercase, letter-spacing 0.24em — wider than other templates
- Body: 17px sans, line-height 1.8 (1.8 NOT 1.65 — luxury uses generous leading)
- Small text: 13px sans, weight 500

## 3. Spacing

The breath is the design. Section vertical rhythm is **160px desktop**,
**96px mobile**. (Other templates use 96/64 — luxury uses MORE.) Hero
and final CTA bumped to 200/120.

Padding inside cards: 56px (not 32-40 like other templates).

## 4. Layout

- **Hero**: full-bleed photo, full viewport height (100vh). Centered text overlay, NOT bottom-left like modern_outdoor. Dark gradient overlay, but lighter (rgba 0.3 → 0.7) — the photo stays visible.
- **No trust strip**. Replaced with a single-line **promise statement** below the hero — small caps eyebrow + larger serif phrase + tiny credentials. Centered.
- **Services**: 2-column grid (NOT 3) — fewer services, more space each. Each tile uses thin hairline border, no background fill.
- **About**: full-width single-column, centered, max-width 760px. Pulled-quote first paragraph in italic serif. No image collage — just one large editorial image below the text.
- **Gallery**: 4-up uniform grid, sharp corners (0px radius). Photos in muted color treatment (CSS filter: saturate 0.92).
- **Testimonial**: centered, single quote, italic serif 48-64px. NO author photo. Subtle attribution beneath in small caps.
- **CTA section**: cream background, centered ink text + outlined button (not filled). NEVER full-bleed primary color.

## 5. Components

### Buttons
- Primary: **outlined**, not filled. Border 1px primary, text primary, padding 16px 40px. Hover: invert to filled.
- Letter-spacing 0.12em on button text — wider than other templates.
- NEVER rounded-pill. NEVER drop-shadow.

### Photos
- Sharp corners (0px radius) — luxury rejects rounded corners.
- Subtle desaturation (filter: saturate 0.92) — feels more editorial.
- No hover zoom. No box-shadow. Just sit there.

### Section dividers
- 1px hairline `var(--mist)` between major sections. NEVER full-color blocks like modern_outdoor uses.

### The promise statement (replaces trust strip)
- Single horizontal centered line, 80-120px tall.
- Format: `<eyebrow caps> A trusted clinic since 1998 <em-dash> NJ Board Certified <em-dash> 1,500+ procedures performed`
- No giant numbers. Restrained credentials only.

## 6. Motion

NONE. No hover-zoom on photos, no ken-burns on hero, no fade-ins.
Only motion: 0.2s ease background color change on button hover.

Luxury feels still. Animation reads as marketing-trying-too-hard.

## 7. Voice / tone

- **Restrained, not pushy.** Never "Book now!" or "Limited time!"
- **Implied confidence.** "We've quietly served Naples since 1998." NOT "5-star rated!"
- **Specific credentials.** "Board-certified plastic surgeon" NOT "expert team".
- **Italic for emphasis**, never UPPERCASE.
- **Second person sparingly.** "Your face" appears once or twice max — luxury uses indirect address ("Patients who choose our practice find...").
- **No numbers in headlines.** Luxury isn't transactional.
- **Hero headline = an aspiration in their words.** Not a service. Not a price. Not a CTA.

## 8. Brand guidelines

- The homepage is a STATEMENT not a SALES PIPELINE. Goal: position the practice as the obvious premium choice, earn a measured inquiry. Not a same-day booking.
- The footer always says "Designed by {Agency Name}" — discreet, small caps.
- Top-of-page banner: subtle. "Preview · {Agency Name} for {Business Name}" — italic, small. NOT a colored alert bar.

## 9. Anti-patterns

DO NOT generate:

- Stock photos of models/handshakes/before-afters that aren't theirs
- Pricing of any kind
- Countdown timers, "limited spots", "this week only"
- Chat widgets
- Loud CTAs ("BOOK NOW!", "CLICK HERE!", "GET STARTED!")
- Star-rating widgets in the hero
- "Why choose us?" sections with checkmarks
- Newsletter signups
- "As featured in" logos we haven't verified
- Carousels
- Gradients with anything other than neutral overlays
- Rounded corners on photos (sharp only)
- More than 4 sentences in any single paragraph
- The word "solutions"
- The word "experience" as a verb ("Experience luxury skincare")
- Exclamation points anywhere
