---
name: Gateway Studio KH
description: Native macOS control panel for one OpenAI-compatible gateway in front of local runners and cloud providers
colors:
  label: "labelColor"          # primary text, appearance-aware
  secondary-label: "secondaryLabelColor"
  tertiary-label: "tertiaryLabelColor"
  separator: "separatorColor"
  control-accent: "controlAccentColor"
  status-ok: "systemGreen"
  status-warn: "systemOrange"
  status-bad: "systemRed"
  status-off: "systemGray"
  status-info: "systemBlue"
typography:
  page-title:
    fontFamily: "SF Pro (system)"
    fontSize: "26px"
    fontWeight: 600
  section:
    fontFamily: "SF Pro (system)"
    fontSize: "15px"
    fontWeight: 600
  row-title:
    fontFamily: "SF Pro (system)"
    fontSize: "13px"
    fontWeight: 600
  body:
    fontFamily: "SF Pro (system)"
    fontSize: "13px"
    fontWeight: 400
  secondary:
    fontFamily: "SF Pro (system)"
    fontSize: "12px"
    fontWeight: 400
  caption:
    fontFamily: "SF Pro (system)"
    fontSize: "11px"
    fontWeight: 400
  caption-strong:
    fontFamily: "SF Pro (system)"
    fontSize: "11px"
    fontWeight: 600
  figure:
    fontFamily: "SF Pro (monospaced digits)"
    fontSize: "22px"
    fontWeight: 600
  mono:
    fontFamily: "SF Mono (system monospaced)"
    fontSize: "12px"
    fontWeight: 400
rounded:
  control: "6px"
  card: "8px"
  sheet: "12px"
  pill: "999px"
spacing:
  xs: "4px"
  tight: "6px"
  s: "8px"
  m: "12px"
  l: "16px"
  xl: "24px"
  page: "28px"
components:
  sidebar:
    width: "220px"
  sidebar-row:
    height: "28px"
    rounded: "{rounded.control}"
  control-small:
    height: "22px"
    rounded: "{rounded.control}"
  control-regular:
    height: "28px"
    rounded: "{rounded.control}"
  card:
    backgroundColor: "controlBackgroundColor"
    rounded: "{rounded.card}"
    padding: "12px 16px"
  badge:
    height: "20px"
    rounded: "999px"
    padding: "0 8px"
  status-badge:
    backgroundColor: "status tint at 14% alpha"
    textColor: "status text tone"
    rounded: "999px"
    padding: "0 8px"
  button-primary:
    backgroundColor: "{colors.control-accent}"
    textColor: "white"
    rounded: "{rounded.control}"
    height: "28px"
---

# Gateway Studio KH design system

## Overview

Operate-mode surface for one owner supervising a local gateway. Native AppKit pages share one token
set (`DS`, `TextRole`, `Health` in `menubar/PageKit.swift`); the web console inside the same window
mirrors the same hierarchy and colours. Two sibling apps (Gateway, Studio Hub) use the same scale so
they read as one product. Everything follows the macOS appearance; there is no custom palette and no
second type family.

## Colors

Colours are macOS semantic colours, resolved per appearance — never fixed hex. Text is `labelColor`
with `secondaryLabelColor` and `tertiaryLabelColor` for hierarchy; separators use `separatorColor`.

Colour carries state only, never decoration:

| Token | Meaning | Use |
|---|---|---|
| `status-ok` (systemGreen) | fine | running, succeeded, online |
| `status-warn` (systemOrange) | look soon | degraded, stale, retrying |
| `status-bad` (systemRed) | act now | failed, offline, error |
| `status-off` (systemGray) | idle or unknown | stopped, not configured |
| `status-info` (systemBlue) | a fact, never an alarm | counts, neutral links |
| `control-accent` | the one accent | primary button, selection, focus |

Status text is the base colour blended 40 % toward the label colour (20 % for idle) so one token
reads in both light and dark mode; never put raw `systemOrange` text on a card.

## Typography

One family, SF Pro, with an eight-role scale and no half points: page title 26, section 15, row
title 13 semibold, body 13, secondary 12, caption 11, caption-strong 11 semibold. Figures use
monospaced digits at 22 so counts do not jitter; `mono` is reserved for identifiers (build hashes,
model ids, keys) and never used as a "technical" costume for ordinary text.

## Layout

4-point grid through `DS.Space`: 4, 6 (the one half-step, title-to-subtitle and label-to-control),
8, 12, 16, 24, 28 page padding. Sidebar is 220 pt, nav rows 28 pt, small controls 22 pt, regular
controls 28 pt, badges 20 pt. Tight groups with generous separation between sections; the page
scrolls, the sidebar does not.

## Elevation & Depth

Flat by default. Depth comes from `separatorColor` hairlines and a 1 px border on cards, not from
shadows or blur. Status badges use a 10–14 % tint fill with a 35 % border of the same hue.

## Shapes

Rounded rectangles only: 6 pt for controls, fields, thumbnails and table containers; 8 pt for cards,
panels, banners and previews; 12 pt for sheets; fully rounded (999) for pills, badges and the version
pill. Icons are SF Symbols, never emoji or text glyphs.

## Components

- **Sidebar**: section headers in caption, rows with a leading SF Symbol and a 13 pt title; the
  selected row uses the accent fill with white text; footer entries carry icons too.
- **Status badge**: symbol + word + tint, so state never depends on colour alone.
- **Primary button**: accent fill, one per view; secondary actions are bordered controls.
- **Cards**: `controlBackgroundColor` fill, 1 px separator border, 8 pt radius, 12/16 padding.
- **Empty and loading states**: a skeleton of the real layout while known content loads, then an
  empty state that names the next action; never a spinner-only or blank panel.

## Do's and Don'ts

- Do use the token (`TextRole.body`, `DS.Space.l`, `Health.warn`); never type a point size or colour.
- Do keep body and placeholder text at 4.5:1 or better in every appearance mode.
- Do give every control default, hover, focus, active, disabled, loading and error states.
- Do keep one accent, for primary action, selection and state only.
- Don't add gradient text, decorative blur or glass, nested cards, eyebrow labels above headings, or
  decorative motion.
- Don't introduce a second type family, a display font in labels or data, or monospace for ordinary
  prose.
- Don't swap the component framework or design language; quality comes from craft on these tokens.
