# Modern Outdoor — Design System

> Brand contract for outdoor / home-service businesses (pool builders,
> landscapers, deck builders, outdoor-kitchen contractors, hardscape).
> Confident, photo-led, conversion-focused without being pushy.

## 1. Color palette

```
--ink:        #0c1612    // Deep forest — headings, primary text
--ink-soft:   #2a3a30    // Body text
--paper:      #ffffff    // Background base
--cream:      #f8f6f1    // Warm panel background
--moss:       #2d5a3d    // Primary brand — buttons, accents
--moss-deep:  #1f3f2b    // Hover state for primary
--gold:       #c89344    // Sunset accent — used sparingly for emphasis
--mist:       #e6ebe7    // Borders, dividers
--shadow:     rgba(12, 22, 18, 0.08)   // Subtle elevation
```

The palette pulls from outdoor materials: deep forest greens, weathered
gold (sunset on stone), warm cream (wood grain). Avoid pure black or
saturated brights — they read as web-design template, not as a brand
that's been outdoors for 30 years.

## 2. Typography

- **Display** (hero, section headlines): **Fraunces** — serif with
  optical sizes. Weight 600. Generous letterspacing on display sizes.
- **Body**: **Inter** — sans-serif. Weight 400 for body, 500 for inline
  emphasis, 600 for buttons + nav.
- **Accent / numerals**: **Inter** with tabular-nums.

Hierarchy:
- Hero headline: 56-72px Fraunces 600
- Section headline: 40-48px Fraunces 600
- Body: 17px Inter 400, line-height 1.65
- Small / labels: 13px Inter 500, uppercase, letter-spacing 0.08em

## 3. Spacing

8px base scale. Most padding/margin uses 16/24/32/48/64/96/128.

Section vertical rhythm: 96px on desktop, 64px on mobile, with the hero
+ final CTA section bumped to 128/96 for emphasis.

## 4. Layout

- Single-column, full-bleed sections. Max content width: 1120px.
- Hero: full-viewport-height, photo background with dark-to-transparent
  gradient overlay (bottom-left dark).
- Trust strip: 3-column horizontal stat row, no card chrome, just numbers.
- Services: 3-column grid (1 column mobile), each tile cream background,
  no border, generous padding.
- About: 2-column split (text + image), reverses to image-first on mobile.
- Gallery: 3-column masonry with rounded corners (12px) and shadow on
  hover. 6-9 images max.
- Testimonials: centered single quote at a time, large italic Fraunces.
- CTA section: full-bleed moss background with cream text and gold CTA.

## 5. Components

### Buttons
- Primary: `bg moss` / `text cream` / `padding 14px 32px` / `border-radius 4px` /
  `font 16px Inter 600` / hover `bg moss-deep`. Never gradient. Never
  rounded-full.
- Secondary: text-only with `border-bottom 2px gold` / hover `border ink`.

### Photos
- Always rounded 12px on cards, sharp on hero (full-bleed).
- 4:3 aspect ratio default for service tiles. 16:9 for gallery.
- Subtle 0 0 24px shadow on hover for clickable images.

### Sections dividers
- None. Use whitespace + background color shifts (paper → cream → moss).

### Trust badges
- Stat = big serif number (Fraunces 48px). Label below in small caps.
  Example: "**12** YEARS BUILDING IN PHOENIX".

## 6. Motion

Static page. No carousels, no scroll-jacking, no AOS-style fade-ins.
A small `transition: opacity 0.15s ease` on hover for buttons + photos.
That's it. The instant feeling beats animation.

## 7. Voice / tone

Writing rules for the LLM populating slots:

- **Specific over generic.** "Built 47 backyards in Mesa" beats "Trusted
  local builder."
- **Their language, not industry jargon.** "Pool" not "aquatic feature."
  "Backyard" not "outdoor living space."
- **Confident, not aggressive.** Avoid "GET A QUOTE NOW!" — use
  "See if we're a fit" or "Talk through your project."
- **No superlatives unless backed by data.** "5-star rated on Google"
  is fine (verifiable). "Best in town" is not.
- **Second person, but not pushy.** "Your backyard" appears often.
  "You deserve" / "You won't believe" never appear.
- **Hero headline = outcome the prospect wants**, in their words. Not
  a tagline.

## 8. Brand guidelines

- The homepage is a TEASER not a sales page. Goal: trigger curiosity,
  earn a 15-min call. Not close.
- Every section ends with the prospect's reader thinking "I want to see
  more." Not "I'm ready to buy."
- The footer always says "Powered by {Agency Name}" — small, near the
  copyright. The preview is positioned as the agency's design work, not
  as the prospect's actual site.
- A single banner at the very top: "This is a preview of what your
  real site could look like. Built in 60 seconds by AI for review."

## 9. Anti-patterns

DO NOT generate:

- Stock-photo hands-shaking handshakes, fake meeting room photos
- "Why choose us?" sections with checkmarks
- Glassmorphism, gradients with purple, neon shadows
- Card stacks with rotation
- Hero videos
- Chat widgets
- Newsletter signup forms
- "As seen on" / "Trusted by" with logos we can't actually verify
- More than ONE primary CTA per scroll
- The word "solutions" anywhere
- Any pricing
- Lorem ipsum or placeholder text — every word must be real, derived
  from the business data we feed in
