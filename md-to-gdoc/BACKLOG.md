# md-to-gdoc backlog

Implementation items found by converting four recent analysis reports (2026-10-08, dev-repo script == productivity-core 0.15.1) and comparing each Doc against its source — Docs API read-back plus a page-by-page visual check of the PDF export.

| Fixture | Source (`claude-bq-agent/analysis/projects/…/report.md`) | Doc |
|---|---|---|
| caitech | `caitech/2026-09-07_ODSB-20057_x_campaign_no_impression` | https://docs.google.com/document/d/1To_Zy9TVh477CswD8FNIiVqX3ywriqf6kVIm6RW_91E/edit |
| canary | `CANARY/2026-10-05_ODSB-20383_trima_click_inflation` | https://docs.google.com/document/d/1WZWWQOJxar_6QbSTMTWygcO7lfZoaVp52tZzmyFU1bo/edit |
| linemanga | `echomarketing/linemanga/2026-10-05_q4_plan` | https://docs.google.com/document/d/11hgilzkd2YNNZ1_Zoset2QtjT-0ZCyUdiNwXRSbkIRA/edit |
| qbr | `apx1/2026-10-02_line_global_qbr` | https://docs.google.com/document/d/1Kxv16T_3esEbwLWYZROwX1rdN4hsl4Y3zEyW9muLeoU/edit |

No content loss, markdown leakage, or leftover sentinels in any of the four.

When an item is fixed, add a reproducing case to `testdata/regression_sample.md` and tick it here.

## A. Bugs

- [ ] **A1. No retry on transient Drive errors** (high). `files().create` returned HTTP 500 `internalError` on 2 of 3 uploads of qbr; the script crashes with a traceback. Fix: pass `num_retries` to `.execute()` (googleapiclient retries 5xx/429 with exponential backoff) on the upload and on `batchUpdate`.
- [ ] **A2. Fuzzy linkify turns bare domains into links** (medium). `Gate.io is excluded…` (qbr) became a link to `http://Gate.io`. Cause: markdown-it `gfm-like` linkify with fuzzy links on — not Docs. Fix: disable fuzzy link / fuzzy email so only `http(s)://` and `www.` autolink.
- [ ] **A3. Relative links become dead links** (medium). `[…](../2026-09-10_deeplink_effectiveness/report.md)` (linemanga) is a blue link to a relative path that cannot resolve in Docs. Fix: for hrefs without a scheme (`http`, `https`, `mailto`), drop the `<a>` and keep the text.
- [ ] **A4. Inline code inside a table header cell** (medium). `` Signal type (ACS `nc.priority`) `` (canary) keeps the pink background + red text on the deep-teal header — reads as a white patch. Fix: skip inline-code styling (or render it white mono, no background) for ranges inside header rows.
- [ ] **A5. SQL `--` → `/* */` rewrite: empty comments and lost indentation** (low–medium).
  - A bare `--` separator line becomes `/* */` (10× in caitech). Fix: emit an empty line, or drop it.
  - Leading spaces after `--` are stripped: `--   2 = ADX …` and column-aligned `--            original_json.reviews…` lose their alignment. Fix: keep the whitespace after `--` inside the `/* */`.
- [ ] **A6. Code-styling interactions** (low).
  - A linked inline code span (`` [`imp_capper.md`](…) ``, caitech) is underlined but stays red, so it does not read as a link. Fix: link color wins over the inline-code foreground (or drop the red for linked code).
  - Every code block ends with an empty line inside the gray box (bottom padding > top). Fix: strip the trailing newline from the `<pre><code>` content before sentinel injection.

## B. Layout decisions

- [ ] **B1. Content-aware table column widths** (medium; visible in the Docs UI, all four reports). Every column gets `MAX_CONTENT_WIDTH_PT / n_cols`, so long label columns wrap 3–5 lines while 1–3 character numeric columns stay mostly empty (qbr §2 "Method", canary F7 "Click window…", linemanga). Proposal: allocate width by each column's max/median visible text length (CJK chars counted ×2), with a per-column floor and the total capped at the content width.
- [ ] **B2. Paragraph spacing** (decision needed). Readers see loose, almost double-spaced paragraphs, because blank source lines are kept as empty paragraphs on top of `spaceBelow=4pt` — a deliberate match of Google's markdown importer. Options: keep as is / drop the empty paragraphs and rely on `spaceBelow` only.
- [ ] **B3. `<br>` metadata lines become separate paragraphs** (low). The `**Author:** …<br>` header block renders as five paragraphs with paragraph spacing, not tight line breaks. Drive's HTML importer splits on `<br>`; would need a sentinel + post-process to rejoin with soft line breaks (`\v`).
- [ ] **B4. Long code lines wrap to column 0** (low). Wrapped SQL lines lose their indentation (more visible in PDF). Options: smaller code font, or accept.

## C. PDF export only (not visible in the pageless Docs UI)

Only worth doing if Docs created by this skill get shared as PDF.

- [ ] **C1. Tables wider than the Letter text column.** Tables are sized to the 665pt pageless width; Letter export has 468pt of text width, so every page holding a table is scaled down ~20% (font size jumps page to page; figures look inconsistently sized as a side effect). Option: cap table width at the paged text width, or set page size/margins so 665pt fits.
- [ ] **C2. Japanese text inside bold runs renders non-bold in the PDF** (canary: `トリマ` in bold paragraphs and H3s). The runs do carry `bold: true` in the Doc JSON, so this looks like PDF font fallback. Not yet checked in the Docs UI.
