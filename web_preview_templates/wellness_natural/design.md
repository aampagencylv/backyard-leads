# Wellness Natural — Design System

> Brand contract for soft-tier service businesses: salons, day spas,
> hair studios, organic beauty, naturopath / functional medicine,
> herbalists, yoga / pilates studios, holistic wellness, doulas,
> indie skincare, plant-based / organic restaurants.
>
> Calm, approachable, organic. The reader should feel like they
> exhaled while looking at the page.

## 1. Color philosophy

Soft warm neutrals + the prospect's brand color rendered LIGHTER than
in other templates (we tint primary toward pastel).

- **Paper** (`#fbf9f4`) — body background. Warm cream-white.
- **Cream** (`#f3eee4`) — section background.
- **Sage** (`#e6e9df`) — secondary panel — desaturated calm green.
- **Ink** (`#2a2823`) — headings. Warm dark brown, NOT pure black.
- **Ink soft** (`#5e564d`) — body text.
- **Mist** (`#dad3c5`) — borders.
- **Primary** — prospect's brand color (often sage, blush, terracotta, dusty blue).
- **Primary deep** — auto-darkened.

The palette feels like a yoga studio bathroom: warm, calm, considered.

## 2. Typography

Default pairing: **Lora + Source Sans 3**.

Lora is humanist — calligraphic, warm, NOT cold like Cormorant.
Source Sans is neutral and friendly — readable at small sizes.

- Hero headline: 56-80px serif, weight 500 (light hand)
- Section headline: 38-52px serif, weight 500
- Eyebrow: 11px sans, uppercase, letter-spacing 0.16em
- Body: 17px sans, line-height 1.75 (generous, calm)
- Small: 14px sans

## 3. Spacing

Section vertical rhythm: 128px desktop, 80px mobile. Between luxury (160) and modern_outdoor (96).

## 4. Layout

- **Hero**: full-bleed photo, centered text. Calmer overlay than luxury — softer gradient (rgba 0.20 → 0.55). Hero photo often has soft natural light (curated by photo curation pass).
- **Promise statement** below hero: NOT trust numbers. Single sentence in italic serif — like luxury's promise, but warmer wording.
- **Services**: 3-column grid with ROUNDED 16px corners on cards. Each card has a subtle accent-color top border (3px). Photo above title.
- **About**: text-left, photo-right. Photo is round-cornered with a soft inner shadow. Bio reads first-person if appropriate ("I founded this practice...").
- **Approach / Philosophy section**: NEW SECTION. 3 short paragraphs explaining the practice's approach, with small icons or numbered steps. Replaces the "Why us" anti-pattern with something content-driven.
- **Testimonial**: single quote, centered, italic serif. Same as luxury but warmer (Lora reads warmer than Cormorant).
- **Gallery**: 3-column, ROUNDED 16px corners, slight saturation boost (1.05).
- **CTA section**: SAGE-tinted background, centered, soft button.

## 5. Components

### Buttons
- Primary: SOLID FILL bg primary / text paper / padding 14px 32px / border-radius 100px (PILL shape — only template that uses pill).
- Secondary: outlined, primary border + text, same pill shape.
- The pill shape signals "approachable, gentle."

### Photos
- ROUNDED 16-24px corners on cards/about. SOFT.
- Slight saturation boost (filter saturate 1.05) — wellness photos benefit from warm rendering.
- No hover scale; subtle shadow rise OK.

### Section dividers
- 1px hairline mist between sections. Subtle.

### Pills
- Used for badges: "Certified", "Member of", "Trained at" — pill-shaped, sage background, ink text.

## 6. Motion

Quiet. 0.3s ease-out on all transitions. Section entry: gentle fade-up (0.6s).

Wellness can have MORE motion than luxury (the slow pace is on-brand)
but it should feel breath-paced, not bouncy.

## 7. Voice / tone

- **Calm, considered.** First-person plural for clinics ("We believe..."), first-person singular for solo practitioners ("I founded this practice after...").
- **NO health claims.** "Helps with stress management" is fine; "cures anxiety" is not. The system prompt enforces this hard.
- **Specific modalities.** "We offer cranial-sacral and lymphatic drainage" beats "We do massage."
- **Approach-driven, not service-driven.** Talk about HOW you work, not just WHAT you do.
- **Warm second-person.** "Your nervous system" beats "the body's nervous system."
- **Hero headline = a feeling state, in their words.** "Slower mornings. Better afternoons." / "Skincare that doesn't shout." / "Your shoulders deserve a Saturday."

## 8. Brand guidelines

- The homepage is a SOFT LANDING. Goal: visitors feel they could ask a question without commitment. Inquiries come from people who felt safe.
- Footer minimal: practitioner name, location, phone (if appropriate), "Designed by {Agency Name}".
- Top banner: cream-bordered, italic, gentle.

## 9. Anti-patterns

DO NOT generate:

- Medical claims that aren't substantiated ("cures", "treats", "heals")
- "Free consultation!" or any exclamation
- Pricing
- Before/after photos (unless from their actual site)
- "Why choose us?" sections
- Star ratings in hero (in a quiet testimonial section is fine)
- Industry jargon ("modalities" can stay but explain it)
- Hero videos
- Aggressive call-to-action language
- Chat widgets
- Newsletter popups (calm landing, no interruption)
- Stock photos of meditation crystals, lotus flowers, etc. (instant fakery)
- Gradients with anything other than soft neutrals
- More than ONE primary CTA per scroll
- "Holistic" without explaining what that means in their practice
- The word "premium" or "luxury"
- The word "solutions"
- Words like "transform" or "journey" or "ritual" (overused in wellness, reads as cliché)
