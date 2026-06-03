# Craft Studio — Design System

> Brand contract for portfolio-driven, design-forward businesses:
> design studios, photographers, custom furniture makers, ceramicists,
> tattoo artists, modern boutique cafés, indie record labels,
> contemporary architects, branding agencies.
>
> Magazine, not marketing page. The work IS the pitch. Copy is sparse.

## 1. Color philosophy

Restrained near-monochrome anchored on the prospect's brand color.
Black/cream/off-white skeleton, primary color used SPARSELY as an
accent (links, key labels, hover states only).

- **Paper** (`#fafaf7`) — body background. Off-white, slightly warm.
- **Cream** (`#f0ede5`) — secondary panels.
- **Ink** (`#0a0a09`) — headings + body. Near-black.
- **Steel** (`#5e5d58`) — captions, metadata.
- **Mist** (`#d8d6cf`) — hairlines.
- **Primary** — extracted from prospect's logo. Used only for: links, eyebrow labels on focused sections, button border/text on hover.
- **NO secondary accent.** Whitespace IS the accent.

## 2. Typography

Default pairing: **DM Serif Display + DM Sans**.

The whole template is built on EXTREME SCALE CONTRAST — display type is
HUGE (96-160px), body is small (15-16px). Captions go down to 10-11px
with wide letter-spacing. This is the "magazine" feel.

- Hero headline: 96-160px display, weight 400 (NOT 600 — heavy display fonts at large sizes don't need extra weight)
- Section headline: 56-88px display, weight 400
- Body: 16px sans, line-height 1.55
- Caption: 10px sans, weight 500, uppercase, letter-spacing 0.32em
- Numerals in features: 13-14px tabular

## 3. Spacing

Section vertical rhythm: 144px desktop, 80px mobile. Generous but not luxurious.

Critical: HEADER OVERLAP. The hero photo extends UNDER the header — header has transparent background and the photo bleeds to top of viewport. Header text is positioned absolute over the photo.

## 4. Layout

- **Header**: transparent over hero, sits on top of the photo. Black text on light photos / white text on dark photos (we pick based on hero brightness — for now default to dark text + add a subtle white shadow).
- **Hero**: 100vh, full-bleed photo. Text positioned BOTTOM-LEFT in tiny caption + giant headline beside it. NOT centered. Asymmetric.
- **Index strip** (instead of trust strip): a horizontal scroll-friendly row showing 4-6 work categories with small labels — "01 — INTERIORS / 02 — KITCHEN / 03 — EXTERIORS / 04 — ARCHITECTURAL DETAILS". No numbers/stats.
- **Work grid**: dominant section. Asymmetric — first work item is full-width, then 2-up, then 3-up, then back to full-width. Like a magazine spread.
- **About**: large editorial portrait + minimal text. Bio-style first-person where appropriate.
- **Testimonial**: NONE. Replaced with PRESS / RECOGNITION strip (publications featured in, awards). If we don't have real press, omit entirely.
- **Final CTA**: minimal. "Tell us about your project →" centered in 56px display. No button. The arrow IS the button.

## 5. Components

### Buttons
- We barely use traditional buttons. Most "buttons" are TEXT LINKS with an underline + arrow.
- Format: `Text →` — text in ink, hover changes text to primary AND underline thickens.
- Reserved button: hero CTA, which uses BLACK background / cream text / sharp 0px corners.

### Photos
- SHARP corners (0px radius). Magazine has no round corners.
- Subtle saturation (filter: saturate 1.02). Magazine printing pops color.
- On hover: brightness shifts slightly (filter brightness 1.05). NO scale.
- Captions BENEATH photos in small caps + project metadata (year, location, type) — like a real portfolio.

### Section dividers
- Big numbers as section markers: `02 / WORK` aligned to top-left of each section in 24px display + small caps label.

## 6. Motion

Quiet but considered. Hover transitions 0.25s ease. Section entry: subtle fade-up
(opacity + 20px translate, 0.5s) — this is the ONE place we allow animation.

Magazine pages don't move, but they have considered weight.

## 7. Voice / tone

- **Tight. Caption-style.** "Brooklyn brownstone. 2024." beats "We renovated this beautiful brownstone in Brooklyn last year."
- **Bio-style about, first-person if appropriate.** "I started Studio X in 2019 after twelve years at..."
- **Concrete materials and processes.** "Plaster. Brass fixtures. White oak." beats "Quality materials."
- **NO sales language whatsoever.** Studios don't sell — they show. Sales-y copy in this template feels embarrassing.
- **Hero headline = single bold statement.** Often a single word or short fragment: "Houses, mostly." / "Light, then form." / "What's left after you remove everything."

## 8. Brand guidelines

- The homepage is a PORTFOLIO not a service menu. Goal: signal taste + capability. Inquiries come from people who already saw enough to know they want to work together.
- The footer is minimal: studio name, location, single contact line, "Designed by {Agency Name}".
- Top-of-page banner: discreet. Same treatment as luxury.

## 9. Anti-patterns

DO NOT generate:

- "Services" sections with bullet lists
- Star ratings or testimonials (NEVER — magazine pages don't have customer quotes)
- Phone numbers in hero or large type (in footer is OK)
- "Get a quote" or "Free consultation" language
- Trust badges, certifications, BBB
- Stock photos (will read instantly as fake)
- Carousels, sliders, animation that delays content
- "Welcome to..." or "About us..." headers
- Pricing or anything number-based about cost
- Newsletter signups
- More than 8 services / categories (3-6 is right)
- Stretched layout (max-width 1180px enforced)
- Gradients of any kind
- Drop shadows
- Rounded corners on anything
- Exclamation marks
- The word "premium", "luxury", "boutique", "bespoke", "curated"
