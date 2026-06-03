# Emergency Service — Design System

> Brand contract for urgency-driven businesses: 24/7 plumbing,
> water-damage restoration, towing, locksmith, biohazard cleanup,
> board-up, mold remediation, fire damage, emergency electrical.
>
> Bold, fast-loading, phone-first. The visitor has a problem RIGHT NOW.
> The page exists to make them call within 5 seconds.

## 1. Color philosophy

High-contrast, alarm-aware. Default to RED unless brand color overrides.

- **Paper** (`#ffffff`) — body background.
- **Ink** (`#0a0a0a`) — headings, primary text. Near-black.
- **Ink soft** (`#444444`) — body.
- **Mist** (`#e0e0e0`) — borders.
- **Primary** — prospect's brand color, or `#cc1f1f` (red) by default.
- **Primary deep** — auto-darkened for hover.
- **Alert** (`#fbb800`) — accent for "24/7 AVAILABLE NOW" / "1-Hour Response" badges.

Limit color palette to 3-4 visible colors. Visitors panicking can't parse a complex visual.

## 2. Typography

Default pairing: **Archivo Black + Archivo** (heavy block display + clean grotesk body).

- Hero headline: 56-88px Archivo Black, weight 900
- Section headline: 36-52px, weight 800
- Eyebrow: 12px sans, uppercase, letter-spacing 0.1em, weight 700
- Body: 16-17px sans, weight 400-500, line-height 1.55
- BIG phone number: 40-72px, tabular-nums, weight 800

## 3. Spacing

Compact. Sections 72-88px desktop, 56-64px mobile. NOT generous —
visitor needs to find the call button in 2 seconds, not breathe.

## 4. Layout

- **Top bar**: full-width primary-color band ABOVE the header. Single line: "🚨 24/7 Emergency — Call (555) 123-4567 — Average response: 18 minutes". This bar is sticky on scroll.
- **Header**: just the brand + phone CTA. Phone CTA is HUGE.
- **Hero**: solid color block (not over photo), centered. Big headline, big phone button, supporting subhead. Photo is SMALL background pattern or sidebar — NOT the focus. The PHONE NUMBER is the focus.
- **Trust strip**: BIG stats — "Response time avg 18 min", "Licensed & Insured", "BBB A+ rated", "Available 24/7/365"
- **Services**: 3-up grid. Each tile has icon (or numeric badge) + service name + 1-sentence problem statement + small "Call Now" button per tile.
- **About / Why call us**: brief, 2-3 sentences max. Focuses on response time + credentials, NOT story.
- **Coverage map / area**: list of zips/cities served + emergency response time.
- **Process**: "1. Call (number) 2. We dispatch within X minutes 3. Tech arrives". 3-step horizontal.
- **CTA section**: ALARM-LEVEL — primary color background, biggest phone number on page, ONE giant call button.

## 5. Components

### Phone CTA button
- BIG: padding 20px 48px, font-size 22px, weight 800.
- Always has 📞 icon prefix.
- Always tel: linked.
- ALWAYS visible above the fold.

### Buttons
- Primary: SOLID FILL primary / text white / weight 800 / padding 16px 40px / border-radius 6px.
- The "Call" button is the ONLY button style that matters. Other buttons (Form, Email) are de-emphasized.

### Alert pills
- "24/7 AVAILABLE" → bg alert yellow, ink text, weight 800, pulsing animation OK (subtle).
- "AVG 18 MIN RESPONSE" → bg alert yellow.

### Photos
- Used sparingly. No hero photo — solid color block is the hero.
- 6-8px rounded corners. Practical, not editorial.
- No filters.

## 6. Motion

ONE allowed animation: subtle pulse on the "24/7 AVAILABLE NOW" badge
(2s ease infinite, slight scale 0.98 → 1.0). This signals "this is happening NOW."

NO scroll animations, NO ken-burns, NO carousels. Visitor needs information now.

## 7. Voice / tone

- **Direct. Imperative. Specific.**
  - "Call (number) — we'll be there in 18 minutes."
  - "Burst pipe? We dispatch in 7 minutes flat."
- **Time-aware specifically.** Generic "fast response" is weak; "average 18-min response in Phoenix metro" is strong.
- **State the problem first.** Hero headline = the problem the visitor has, in their words.
  - "Burst pipe at 2am?"
  - "Water on the floor?"
  - "Need to be there yesterday?"
- **Credentials in the hero.** License #, BBB rating, years in business — visitor needs reassurance the random panic-search led somewhere legitimate.
- **No upselling.** Don't mention non-emergency services in the hero. The visitor is here for one thing.

## 8. Brand guidelines

- The homepage is an URGENT FUNNEL. Goal: phone call within 5 seconds. Anything that delays that = drop-off.
- Footer minimal: phone, license #, service area, hours, "Designed by {Agency Name}".
- Top banner persistent: emergency hotline.

## 9. Anti-patterns

DO NOT generate:

- Hero videos
- Carousels of any kind
- "Why choose us?" sections
- Long About paragraphs (>2 sentences)
- Pricing (visitors in panic don't comparison-shop)
- Newsletter signups
- Generic stock photos of trucks/tools
- Marketing-speak ("revolutionizing", "industry-leading", etc.)
- ANY animation that delays the phone number being visible
- More than ONE primary CTA (it's ALWAYS the phone)
- Form submissions positioned above the phone CTA
- Lists of certifications without numbers (license #, certs in writing)
- "Same-day service" without "average response: XX minutes" backing it up
- Lifestyle imagery
- The word "solutions"
- Emojis other than ☎ 📞 🚨 ⚡ (used as functional icons)
