# MORJAS – Meta catalog feeds

Generates the four files Meta fetches to build the product catalog. Runs hourly
on GitHub Actions; output is served by GitHub Pages from `docs/`.

| File | Purpose | Override column |
|---|---|---|
| `meta_primary_feed.csv` | Item identity, images, title, description | – |
| `meta_country_feed.csv` | Price, link, availability per market | ISO country (`DE`, `US`, …) |
| `meta_language_de_XX.csv` | German title + description | `de_XX` |
| `meta_language_fr_XX.csv` | French title + description | `fr_XX` |

`docs/_status.json` records the last run: timestamp, row counts, and whether the
attribute cache was live or fell back.

## Why this exists

The item id must be **identical in every market** (`p{product}-v{variant}-s`).
The previous catalog used market-scoped ids (`1/SE-p…`), so only Swedish traffic
could ever match the pixel — event match rate sat at 9.3% and dynamic ads were
impossible.

Two things Centra's feed cannot express are applied here:

- **Exclusions by Collection** – `The Archive` (95 items, includes everything
  marked Signs of Wear) and `Shoe Care Collection` (36). Collection exists only
  in the Centra API, never in the feed.
- **Title enrichment** – the localised *Color Swatch* attribute is appended, so
  fifteen products no longer all read "MORJAS The Penny Loafer".

## Operating notes

- **Fail closed.** If a fetch fails or any output falls below its minimum row
  count, nothing is written and the run fails. The previously committed files
  stay live. An empty feed tells Meta to *delete* the catalog.
- **Attribute cache.** `cache/attributes.json` is refreshed each run and
  committed. If the Centra API is down the last good cache is reused, so an
  outage stales titles rather than breaking the feed.
- **Secrets.** `CENTRA_API_URL` and `CENTRA_API_TOKEN` are repository secrets.
  Rotating the Centra token without updating them here will silently stale the
  titles — check `docs/_status.json` for `"attributes": "cached"`.
- **Scheduled runs** fire at :17 past the hour. GitHub disables scheduled
  workflows in repositories with no activity for 60 days.

## Running locally

```bash
CENTRA_API_URL=... CENTRA_API_TOKEN=... python build_feeds.py
```

Standard library only — no dependencies.
