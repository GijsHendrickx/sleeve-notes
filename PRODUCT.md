# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Vinyl DJs and collectors with a Discogs collection. They sit at a desk with a stack of records and a label sheet, preparing sleeves for a gig. Sign-in is Discogs OAuth, so everyone arrives with a collection. (Inferred from README and CLAUDE.md; not yet confirmed by interview.)

## Product Purpose
Turns a Discogs collection into printable sleeve stickers carrying BPM, key and track info. A four-step flow (Records, BPM, Design, Get them) replaces a pipeline of CLI steps. Success is a correct sheet of stickers printed with little fiddling.

## Positioning
Goal-oriented rather than pipeline-oriented: the UI describes consequences ("41 of 54 tracks have a BPM"), never internals ("run the BPM cascade"). Home is a launcher with one primary action, not a metrics dashboard.

## Constraints
- Server-rendered Django templates with Tailwind (CDN) and HTMX; no SPA.
- Voice: plain, task-oriented, full column headers (BPM is the only abbreviation). No italics on song or release titles.
- The sticker preview is the only element that represents the printed object; it sits on a paper colour.

## Brand
Hardware-inspired and modern: light grey housing, a dark display for the key number, one orange LED for what is active. Aimed at vinyl purists without looking old. The name is still open. Full system in `DESIGN.md`.
