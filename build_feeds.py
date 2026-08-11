"""Meta catalog feed generator.

Builds four files into docs/ , which GitHub Pages serves at stable URLs that
Meta fetches hourly:

    meta_primary_feed.csv     item identity  (collection-filtered, swatch-enriched)
    meta_country_feed.csv     price / link / availability per market  (override = ISO)
    meta_language_de_XX.csv   title / description in German           (override = de_XX)
    meta_language_fr_XX.csv   title / description in French           (override = fr_XX)

Inputs
    Centra plugin-export, one feed per market (public, no auth)
    Centra GraphQL API      - Collection + localised "Color Swatch" per product
                              (these exist ONLY in the API, never in the feed)

Cached attributes are committed to cache/attributes.json. If the API is
unreachable the last good cache is reused, so a Centra API outage stales the
titles rather than breaking the feed.

FAIL CLOSED: if any output falls below its minimum row count the script writes
nothing and exits non-zero. The previously committed files stay live. An empty
feed does not merely stall - it tells Meta to delete the catalog.

Environment: CENTRA_API_URL, CENTRA_API_TOKEN  (GitHub repo secrets)
Standard library only - no dependencies to install or keep up to date.
"""

import csv, html, json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
CACHE = os.path.join(ROOT, "cache", "attributes.json")

FEED_BASE = "https://morjas.centra.com/plugin-export/meta-feed-test/{}"
BASE_MARKET = "se"
COUNTRIES = ["de", "at", "fr", "us", "gb", "dk", "no", "nl", "ch", "jp", "au"]
# Meta wants a *locale* in the override column of a language feed, not a bare
# language. de_XX is rejected outright ("Override value isn't supported"), while
# fr_XX is accepted - an inconsistency on Meta's side, so each feed keeps
# whatever it has been proven to accept. Emitting every German locale we sell
# into also gets AT and CH German titles, which de_XX would only have matched.
# File names are kept stable so the configured feed URLs keep working.
LOCALES = {
    "de_XX": {"market": "de", "lang": "DE", "overrides": ["de_DE", "de_AT", "de_CH"]},
    "fr_XX": {"market": "fr", "lang": "FR", "overrides": ["fr_XX"]},
}
EXCLUDE_COLLECTIONS = {"The Archive", "Shoe Care Collection"}
LANGUAGE_IDS = [6, 7]                     # 6 = German, 7 = French
SWATCH_ATTRIBUTE = "Color Swatch"

FIELDS = ["id", "title", "description", "availability", "condition", "price", "link",
          "image_link", "additional_image_link", "brand", "google_product_category",
          "product_type", "item_group_id", "gender", "age_group", "color", "material", "mpn"]

MIN_PRIMARY, MIN_COUNTRY, MIN_LANG, MIN_CACHE = 200, 2000, 200, 300


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------- Centra feeds

def fetch(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if i == tries - 1:
                raise
            log(f"   retry {i+1} after {e}")
            time.sleep(3 * (i + 1))


def load_market(market):
    xml = fetch(FEED_BASE.format(market))
    if len(xml) < 10000:
        raise RuntimeError(f"feed {market} suspiciously small ({len(xml)} bytes)")
    rows = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        d = {}
        for f in FIELDS:
            m = re.search(r"<(?:g:)?%s>(<!\[CDATA\[)?(.*?)(?:\]\]>)?</(?:g:)?%s>" % (f, f), it, re.S)
            if not m:
                d[f] = ""
                continue
            v = m.group(2).strip()
            # Text outside CDATA is entity-encoded. Meta matches
            # google_product_category against Google's taxonomy literally, so
            # "Apparel &amp; Accessories &gt; Shoes" would fail to resolve.
            # CDATA content is already literal - leave it alone.
            d[f] = v if m.group(1) else html.unescape(v)
        m = re.match(r"p(\d+)-v(\d+)-s", d["id"])
        d["p"] = m.group(1) if m else None
        # Centra still emits a market-scoped group id ("1/se-p7825"). Derive it
        # from the canonical id instead, so grouping never depends on which
        # market the primary feed was generated from.
        if d["p"]:
            d["item_group_id"] = "p" + d["p"]
        # Centra ships 1 image_link + 2-10 additional. DFW - and therefore every
        # ad running today - uses additional[0] as the main image. Keep parity and
        # expose the rest to Meta.
        adds = [x.strip() for x in re.findall(
            r"<(?:g:)?additional_image_link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</(?:g:)?additional_image_link>",
            it, re.S)]
        if adds:
            d["image_link"] = adds[0]
            d["additional_image_link"] = ",".join(adds[1:10])
        else:
            d["additional_image_link"] = ""
        if d["id"]:
            rows.append(d)
    log(f"   {market}: {len(rows)} items")
    return rows


# ----------------------------------------------------------- Centra GraphQL

def gql(query, tries=4):
    url, token = os.environ.get("CENTRA_API_URL"), os.environ.get("CENTRA_API_TOKEN")
    if not url or not token:
        raise RuntimeError("CENTRA_API_URL / CENTRA_API_TOKEN not set")
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.loads(r.read())
            if out.get("errors"):
                raise RuntimeError(str(out["errors"])[:300])
            return out["data"]
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


ATTR_QUERY = """query { productConnection(first: 20, where: { id: [%s] }) { edges { node { id
  collection { name }
  attributes(where: { description: { equals: "%s" } }) {
    ... on MappedAttribute { name translations(where: { languageId: [%s] }) {
        language { code } fields { field value } } } } } } } }"""


def build_cache(product_ids):
    """Collection + localised colour swatch per product. Raises on API failure."""
    out = {}
    for i in range(0, len(product_ids), 20):
        batch = product_ids[i:i + 20]
        q = ATTR_QUERY % (",".join(map(str, batch)), SWATCH_ATTRIBUTE,
                          ",".join(map(str, LANGUAGE_IDS)))
        for e in gql(q)["productConnection"]["edges"]:
            n = e["node"]
            rec = {"collection": (n.get("collection") or {}).get("name", "")}
            a = (n.get("attributes") or [None])[0]
            if a and a.get("name"):
                rec["EN"] = a["name"]
                for tr in a.get("translations", []):
                    v = next((f["value"] for f in tr["fields"] if f["field"] == "desc"), None)
                    if v:
                        rec[tr["language"]["code"]] = v
            out[str(n["id"])] = rec
        log(f"   cache {min(i + 20, len(product_ids))}/{len(product_ids)}")
    return out


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0, sort_keys=True)


# --------------------------------------------------------------------- build

def enrich(title, swatch):
    if swatch and swatch.lower() not in title.lower():
        return f"{title} - {swatch}"
    return title


def write_csv(name, header, rows, minimum):
    if len(rows) < minimum:
        raise RuntimeError(f"refusing to write {name}: {len(rows)} rows, minimum {minimum}")
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, name), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    log(f"   {name}: {len(rows)} rows")
    return len(rows)


def main():
    started = datetime.now(timezone.utc)
    log("Fetching Centra market feeds")
    markets = {BASE_MARKET: load_market(BASE_MARKET)}

    # ---- attributes: refresh from the API, fall back to the committed cache ----
    pids = sorted({int(r["p"]) for r in markets[BASE_MARKET] if r["p"]})
    cache, cache_source = None, ""
    try:
        log(f"Refreshing attribute cache for {len(pids)} products")
        fresh = build_cache(pids)
        if len(fresh) < MIN_CACHE:
            raise RuntimeError(f"cache returned only {len(fresh)} products")
        cache, cache_source = fresh, "live"
        save_cache(cache)
    except Exception as e:
        log(f"!! attribute refresh failed: {e}")
        cache = load_cache()
        if not cache:
            log("!! no committed cache to fall back on - aborting, nothing written")
            return 1
        cache_source = "cached"
        log(f"   falling back to committed cache ({len(cache)} products)")

    def swatch(pid, lang):
        rec = cache.get(str(pid), {})
        return rec.get(lang) or rec.get("EN") or ""

    for m in COUNTRIES + [v["market"] for v in LOCALES.values()]:
        if m not in markets:
            markets[m] = load_market(m)

    # ---- primary -------------------------------------------------------------
    kept, dropped = [], {}
    for r in markets[BASE_MARKET]:
        coll = cache.get(str(r["p"]), {}).get("collection", "")
        if coll in EXCLUDE_COLLECTIONS:
            dropped[coll] = dropped.get(coll, 0) + 1
            continue
        r["title"] = enrich(r["title"], swatch(r["p"], "EN"))
        kept.append(r)
    keep_ids = {r["id"] for r in kept}
    base_link = {r["id"]: r["link"] for r in kept}
    log(f"Primary: {len(markets[BASE_MARKET])} -> {len(kept)}   excluded {dropped}")

    # ---- country -------------------------------------------------------------
    country = []
    for mk in COUNTRIES:
        iso, present = mk.upper(), set()
        for r in markets[mk]:
            if r["id"] not in keep_ids:
                continue
            present.add(r["id"])
            country.append([r["id"], iso, r["price"], r["link"], r["availability"]])
        # not sold in this market -> explicit out of stock, so it can never fall
        # back to the base feed's SEK price
        for missing in sorted(keep_ids - present):
            country.append([missing, iso, "", base_link[missing], "out of stock"])

    # ---- language ------------------------------------------------------------
    lang_rows = {}
    for name, cfg in LOCALES.items():
        rows = []
        for r in markets[cfg["market"]]:
            if r["id"] not in keep_ids:
                continue
            title = enrich(r["title"], swatch(r["p"], cfg["lang"]))
            for override in cfg["overrides"]:
                rows.append([r["id"], override, title, r["description"]])
        lang_rows[name] = rows

    # ---- write (guarded) -----------------------------------------------------
    log("Writing")
    counts = {}
    counts["primary"] = write_csv("meta_primary_feed.csv", FIELDS,
                                  [[r.get(f, "") for f in FIELDS] for r in kept],
                                  MIN_PRIMARY)
    counts["country"] = write_csv("meta_country_feed.csv",
                                  ["id", "override", "price", "link", "availability"],
                                  country, MIN_COUNTRY)
    for locale, rows in lang_rows.items():
        counts[locale] = write_csv(f"meta_language_{locale}.csv",
                                   ["id", "override", "title", "description"],
                                   rows, MIN_LANG)

    status = {
        "generated_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "attributes": cache_source,
        "products_in_cache": len(cache),
        "excluded": dropped,
        "rows": counts,
    }
    with open(os.path.join(DOCS, "_status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
    log("OK " + json.dumps(status["rows"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"FAILED: {exc}")
        sys.exit(1)
