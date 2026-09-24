#!/usr/bin/env python3
"""
nyer_ai_tracker.py - Count New Yorker articles about A.I., by month and year.

Two sources, reported separately and combined:
  ONLINE  Articles the magazine tags "Artificial Intelligence" / "Artificial Intelligence (A.I.)",
          found by paging through those tag pages.
  PRINT   Print-magazine pieces, which carry no topic tags. Found by reading each weekly
          issue's table of contents; a piece counts if its headline mentions A.I. or its text
          mentions A.I. at least --mag-threshold times (default 5).

Everything is cached in cache.json, so reruns are fast and an interrupted run resumes.

Usage
  python nyer_ai_tracker.py                        # 2023-01-01 through today, online + print
  python nyer_ai_tracker.py --no-magazine          # online tags only
  python nyer_ai_tracker.py --analyze-only         # recompute stats from the cache
  python nyer_ai_tracker.py --analyze-only --mag-threshold 8   # stricter print rule
  python nyer_ai_tracker.py --discover URL         # diagnose one article

Be polite: the default delay is 1.5 s between requests. This is for personal research.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.newyorker.com"
DEFAULT_TAGS = ["artificial-intelligence-ai", "artificial-intelligence"]
EXCLUDE_SECTIONS = {
    "tag", "tags", "contributor", "contributors", "newsletter", "newsletters", "account",
    "search", "about", "shop", "sitemap", "subscribe", "video", "cartoons", "crossword",
    "puzzles-and-games-dept", "games", "latest", "services", "info", "privacy",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal research script: counting A.I. coverage by month)",
    "Accept-Language": "en-US,en;q=0.9",
}
# What counts as a mention of A.I. in article text (The New Yorker writes "A.I.").
AI_PATTERN = re.compile(
    r"\bA\.I\.|\bartificial[- ]intelligence\b|\bchatbots?\b|\bChatGPT\b"
    r"|\blarge[- ]language[- ]models?\b|\bmachine[- ]learning\b",
    re.IGNORECASE,
)
BODY_CLASSES = re.compile(r"body__inner-container|article__body|ArticleBody|body__container|article-body")


# --------------------------------------------------------------------------- fetching
def fetch(session, url, delay, retries=4):
    """Return page HTML, or None for 404/other permanent failures."""
    err = None
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException as e:
            err = e
        else:
            if r.status_code == 200:
                time.sleep(delay)
                return r.text
            if r.status_code in (429, 500, 502, 503, 504):
                err = f"HTTP {r.status_code}"
            else:
                time.sleep(delay)
                if r.status_code == 403:
                    print(f"  ! 403 Forbidden for {url} (the site may be blocking scripts)",
                          file=sys.stderr)
                return None
        wait = delay * (2 ** (attempt + 1))
        print(f"  ... {err}; retrying in {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)
    print(f"  ! giving up on {url}: {err}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------- parsing
def normalize(url):
    p = urlparse(urljoin(BASE, url))
    if p.netloc not in ("www.newyorker.com", "newyorker.com"):
        return None
    return [s for s in p.path.split("/") if s]


def article_links(html):
    """Candidate article URLs on a tag listing page, in page order."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        segs = normalize(a["href"])
        if not segs or len(segs) < 2 or segs[0] in EXCLUDE_SECTIONS:
            continue
        if "-" not in segs[-1]:
            continue
        url = BASE + "/" + "/".join(segs)
        if url not in out:
            out.append(url)
    return out


def _walk_jsonld(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
    elif isinstance(node, dict):
        yield node
        if "@graph" in node:
            yield from _walk_jsonld(node["@graph"])


def _ymd(s):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def slugify(text):
    """'Artificial Intelligence (A.I.)' -> 'artificial-intelligence-ai'"""
    text = text.lower().replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _body_text(soup, ld_objs):
    """Article text only (not sidebars or 'Read More' lists). Returns (text, where_found)."""
    for obj in ld_objs:
        b = obj.get("articleBody")
        if isinstance(b, str) and len(b) > 500:
            return b, "json-ld"
    containers = soup.find_all(class_=BODY_CLASSES)
    text = " ".join(c.get_text(" ", strip=True) for c in containers)
    if len(text) > 500:
        return text, "body-container"
    ps = [p for p in soup.find_all("p") if not p.find_parent(["nav", "aside", "footer", "header"])]
    return " ".join(p.get_text(" ", strip=True) for p in ps), "all-paragraphs"


def extract_article_info(html):
    """Date, headline, topic tags, and how often the text mentions A.I."""
    soup = BeautifulSoup(html, "html.parser")
    pub, title = None, None
    keywords, ld_objs = [], []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk_jsonld(data):
            ld_objs.append(obj)
            pub = pub or _ymd(obj.get("datePublished"))
            if not title and isinstance(obj.get("headline"), str):
                title = obj["headline"]
            kw = obj.get("keywords")
            if isinstance(kw, str):
                keywords += [k.strip() for k in kw.split(",")]
            elif isinstance(kw, list):
                keywords += [str(k).strip() for k in kw]
    for name in ("keywords", "news_keywords"):
        m = soup.find("meta", attrs={"name": name})
        if m and m.get("content"):
            keywords += [k.strip() for k in m["content"].split(",")]
    for m in soup.find_all("meta", attrs={"property": "article:tag"}):
        if m.get("content"):
            keywords.append(m["content"].strip())
    if not pub:
        m = soup.find("meta", attrs={"property": "article:published_time"})
        pub = _ymd(m.get("content")) if m else None
    if not pub:
        t = soup.find("time", attrs={"datetime": True})
        pub = _ymd(t["datetime"]) if t else None
    if not title:
        m = soup.find("meta", attrs={"property": "og:title"})
        title = m.get("content") if m else (
            soup.title.string.strip() if soup.title and soup.title.string else "")
    tag_paths = set()
    for a in soup.find_all("a", href=True):
        segs = normalize(a["href"])
        if segs and len(segs) == 2 and segs[0] == "tag":
            tag_paths.add(segs[1])
    kw_slugs = {slugify(k) for k in keywords if k} - {""}
    body, where = _body_text(soup, ld_objs)
    return {
        "date": pub, "title": title or "", "tags": sorted(tag_paths), "kw": sorted(kw_slugs),
        "ai_mentions": len(AI_PATTERN.findall(body)), "words": len(body.split()),
        "body_src": where, "title_ai": bool(AI_PATTERN.search(title or "")),
    }


def classify(info, tags):
    """'tagged' = page carries an A.I. tag; 'listed' = untagged but on an A.I. tag page;
    None = not counted by tags."""
    tagset = set(tags)
    if tagset.intersection(info.get("tags", [])) or tagset.intersection(info.get("kw", [])):
        return "tagged"
    pages = info.get("listing_pages", {})
    listed = tagset.intersection(pages)
    if listed and not info.get("tags") and not info.get("kw"):
        if all(len(pages[t]) < 3 for t in listed):  # 3+ pages = menu/sidebar link
            return "listed"
    return None


def text_hit(info, threshold):
    return bool(info.get("title_ai")) or (info.get("ai_mentions") or 0) >= threshold


# --------------------------------------------------------------------------- cache
def load_cache(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def get_article(session, cache, url, delay, need_text=False):
    info = cache.get(url)
    if info is None or (need_text and "ai_mentions" not in info):
        html = fetch(session, url, delay)
        new = extract_article_info(html) if html else {"date": None, "title": "", "tags": [], "kw": []}
        if info:  # keep listing history from earlier runs
            new["listing_pages"] = info.get("listing_pages", {})
        cache[url] = info = new
        return info, True
    return info, False


# --------------------------------------------------------------------------- crawling: tags
def crawl_tags(session, tags, start, cache, cache_path, delay, max_pages):
    for tag in tags:
        print(f"\n=== Online: tag {tag} ===")
        seen = set()
        for page in range(1, max_pages + 1):
            url = f"{BASE}/tag/{tag}" + (f"?page={page}" if page > 1 else "")
            html = fetch(session, url, delay)
            if html is None:
                if page == 1:
                    print(f"  ! Tag page not found: {url}")
                break
            page_links = article_links(html)
            links = [l for l in page_links if l not in seen]
            if not links:
                print(f"  page {page}: no new links, end of tag.")
                break
            seen.update(links)
            fetched = 0
            for link in links:
                _, new = get_article(session, cache, link, delay)
                fetched += new
            for link in page_links:
                lp = cache[link].setdefault("listing_pages", {}).setdefault(tag, [])
                if page not in lp:
                    lp.append(page)
            dates = [cache[l]["date"] for l in links if cache[l].get("date") and classify(cache[l], [tag])]
            save_cache(cache, cache_path)
            rng = f"({min(dates)} to {max(dates)})" if dates else ""
            print(f"  page {page}: {len(links)} links, {fetched} fetched, {len(dates)} tagged {rng}")
            if dates and max(dates) < start:
                print(f"  reached articles older than {start}; stopping this tag.")
                break


# --------------------------------------------------------------------------- crawling: magazine
def issue_dates(start, end):
    d = date.fromisoformat(start)
    d -= timedelta(days=d.weekday())            # issues are dated Mondays
    last = date.fromisoformat(end) + timedelta(days=14)  # issues are dated ahead of release
    while d <= last:
        yield d
        d += timedelta(days=7)


def crawl_magazine(session, start, end, cache, cache_path, delay, threshold):
    issues = list(issue_dates(start, end))
    recent = date.today() - timedelta(days=21)
    todo = [d for d in issues if f"issue:{d}" not in cache or d >= recent]
    print(f"\n=== Print: {len(issues)} possible issue dates, {len(todo)} to check "
          f"(double issues skip weeks, so some will be empty) ===")
    misses = 0
    t0 = time.time()
    for i, d in enumerate(todo, 1):
        key = f"issue:{d}"
        html = fetch(session, f"{BASE}/magazine/{d:%Y/%m/%d}", delay)
        if html is None:
            cache[key] = {"issue": True, "pieces": 0}
            misses += 1
            if misses == 6 and i == 6:
                print("  ! The first six issue pages all failed. The site's issue URLs may have\n"
                      "    changed; send this output to Claude.")
            continue
        prefix = f"/magazine/{d:%Y/%m/%d}/"
        slugs = []
        for m in re.finditer(re.escape(prefix) + r"([a-z0-9][a-z0-9-]*)", html):
            if m.group(1) not in slugs:
                slugs.append(m.group(1))
        hits = 0
        for slug in slugs:
            url = BASE + prefix + slug
            info, _ = get_article(session, cache, url, delay, need_text=True)
            info["mag_issue"] = str(d)
            hits += text_hit(info, threshold)
        cache[key] = {"issue": True, "pieces": len(slugs)}
        save_cache(cache, cache_path)
        elapsed = time.time() - t0
        eta = elapsed / i * (len(todo) - i) / 60
        print(f"  issue {d}: {len(slugs):>2} pieces, {hits} about A.I.   "
              f"[{i}/{len(todo)}, ~{eta:.0f} min left]")


# --------------------------------------------------------------------------- analysis
def months_between(start, end):
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def pct(new, old):
    return f"{(new - old) / old * 100:+.0f}%" if old else "n/a"


def write_csv(path, header, rows):
    # utf-8-sig so Excel shows curly quotes and accents correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def analyze(cache, tags, start, end, outdir, no_verify, strict, include_mag, threshold):
    os.makedirs(outdir, exist_ok=True)
    rows, listed_only = [], 0
    for url, info in cache.items():
        if not url.startswith("http"):
            continue
        d = info.get("date")
        if not d or not (start <= d <= end):
            continue
        section = urlparse(url).path.strip("/").split("/")[0]
        is_print = section == "magazine"
        kind = classify(info, tags)
        if kind is None and no_verify and set(tags).intersection(info.get("listing_pages", {})):
            kind = "listed"
        if strict and kind == "listed":
            kind = None
        by_text = is_print and include_mag and "ai_mentions" in info and text_hit(info, threshold)
        if not kind and not by_text:
            continue
        if kind == "listed" and not by_text:
            listed_only += 1
        if kind == "tagged":
            basis = "tag"
        elif by_text:
            basis = "headline" if info.get("title_ai") else f"text ({info['ai_mentions']} mentions)"
        else:
            basis = "listed on tag page"
        rows.append([d, info.get("title", ""), "print" if is_print else "online", section, basis,
                     info.get("ai_mentions", ""), url])
    rows.sort(key=lambda r: r[0])
    if not rows:
        print("\nNo matching articles found in the date range.")
        return

    write_csv(os.path.join(outdir, "articles.csv"),
              ["date", "title", "channel", "section", "counted_because", "ai_mentions", "url"], rows)

    months = list(months_between(start, end))
    on = Counter(r[0][:7] for r in rows if r[2] == "online")
    pr = Counter(r[0][:7] for r in rows if r[2] == "print")
    tot = [on[m] + pr[m] for m in months]
    roll = [sum(tot[max(0, i - 2):i + 1]) / len(tot[max(0, i - 2):i + 1]) for i in range(len(tot))]
    write_csv(os.path.join(outdir, "monthly_counts.csv"),
              ["month", "online", "print", "total", "total_3mo_avg"],
              [[m, on[m], pr[m], t, f"{r:.1f}"] for m, t, r in zip(months, tot, roll)])

    years = sorted({m[:4] for m in months})
    end_md = end[5:]
    partial = end_md != "12-31"
    yon = Counter(r[0][:4] for r in rows if r[2] == "online")
    ypr = Counter(r[0][:4] for r in rows if r[2] == "print")
    print(f"\nNew Yorker articles about A.I., {start} to {end}: {len(rows)} total")
    if listed_only:
        print(f"({listed_only} counted because they appear on an A.I. tag page without readable tags; "
              f"--strict excludes them)")
    print(f"\n{'Year':<6}{'Online':>8}{'Print':>8}{'Total':>8}{'YoY':>11}")
    annual_rows, prev = [], None
    for y in years:
        o, p = yon.get(y, 0), ypr.get(y, 0)
        t = o + p
        last_partial = y == years[-1] and partial
        change = "see below" if last_partial else (pct(t, prev) if prev is not None else "")
        note = f"partial year (through {end_md})" if last_partial else ""
        if y == start[:4] and start[5:] != "01-01":
            note = f"partial year (from {start[5:]})"
        print(f"{y:<6}{o:>8}{p:>8}{t:>8}{change:>11}  {note}")
        annual_rows.append([y, o, p, t, change, note])
        prev = t
    write_csv(os.path.join(outdir, "annual_counts.csv"),
              ["year", "online", "print", "total", "yoy_change_total", "note"], annual_rows)

    if partial and len(years) >= 2:
        y, py = years[-1], str(int(years[-1]) - 1)
        def ytd(year, ch):
            return sum(1 for r in rows if r[0][:4] == year and r[0][5:] <= end_md and (ch is None or r[2] == ch))
        print(f"\nSame period, {y} vs. {py} (Jan 1 to {end_md}):")
        for label, ch in (("Online", "online"), ("Print", "print"), ("Total", None)):
            a, b = ytd(y, ch), ytd(py, ch)
            print(f"  {label:<7}{a:>5} vs. {b:<5} ({pct(a, b)})")

    full = [y for y in years if not (y == years[-1] and partial)
            and not (y == start[:4] and start[5:] != "01-01")]
    if len(full) >= 2 and (yon[full[0]] + ypr[full[0]]):
        n = int(full[-1]) - int(full[0])
        cagr = ((yon[full[-1]] + ypr[full[-1]]) / (yon[full[0]] + ypr[full[0]])) ** (1 / n) - 1
        print(f"\nCompound annual growth, total, {full[0]} to {full[-1]}: {cagr * 100:+.0f}% per year")

    print(f"\n{'Month':<9}{'Online':>8}{'Print':>8}{'Total':>8}{'3-mo avg':>10}")
    for m, t, r in list(zip(months, tot, roll))[-18:]:
        print(f"{m:<9}{on[m]:>8}{pr[m]:>8}{t:>8}{r:>10.1f}")
    print("(last 18 months shown; full series in monthly_counts.csv)")
    if include_mag:
        print(f"\nPrint rule: headline mentions A.I., or text mentions it {threshold}+ times "
              f"(change with --analyze-only --mag-threshold N).")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        x = range(len(months))
        fig, ax = plt.subplots(figsize=(12, 5))
        ax.bar(x, [on[m] for m in months], color="#9bb7d4", label="Online (tagged)")
        ax.bar(x, [pr[m] for m in months], bottom=[on[m] for m in months], color="#d4a373",
               label="Print magazine")
        ax.plot(x, roll, color="#1f3b5a", lw=2, label="Total, 3-month average")
        step = max(1, len(months) // 16)
        ax.set_xticks(range(0, len(months), step))
        ax.set_xticklabels(months[::step], rotation=45, ha="right")
        ax.set_ylabel("Articles")
        ax.set_title("New Yorker articles about A.I., by month")
        ax.legend()
        fig.tight_layout()
        path = os.path.join(outdir, "ai_articles_by_month.png")
        fig.savefig(path, dpi=150)
        print(f"\nChart saved: {path}")
    except ImportError:
        print("\n(matplotlib not installed; skipping chart)")
    print(f"CSVs saved in: {outdir}")


def discover(session, url):
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"Could not connect: {e}")
        return
    info = extract_article_info(r.text)
    print(f"HTTP status: {r.status_code}   page size: {len(r.text):,} characters")
    if r.status_code != 200 or not info["title"]:
        print("  ! This may be a block or error page rather than the article.")
    print(f"Title:       {info['title']}")
    print(f"Published:   {info['date']}")
    print(f"Tag links:   {', '.join(info['tags']) or '(none)'}")
    print(f"Keywords:    {', '.join(info['kw']) or '(none)'}")
    print(f"Body text:   {info['words']:,} words (found via {info['body_src']})")
    print(f"A.I. mentions in body: {info['ai_mentions']}   headline mentions A.I.: {info['title_ai']}")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2023-01-01", help="YYYY-MM-DD (default 2023-01-01)")
    ap.add_argument("--end", default=date.today().isoformat(), help="YYYY-MM-DD (default today)")
    ap.add_argument("--tags", nargs="+", default=DEFAULT_TAGS, help="tag slugs to count")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    ap.add_argument("--max-pages", type=int, default=400, help="safety cap per tag")
    ap.add_argument("--out", default="output", help="output folder")
    ap.add_argument("--cache", default="cache.json", help="cache file")
    ap.add_argument("--analyze-only", action="store_true", help="skip crawling, use the cache")
    ap.add_argument("--no-magazine", action="store_true", help="skip the print-magazine pass")
    ap.add_argument("--mag-threshold", type=int, default=5,
                    help="A.I. mentions needed for a print piece to count (default 5)")
    ap.add_argument("--strict", action="store_true",
                    help="only count online articles whose own page carries an A.I. tag")
    ap.add_argument("--no-verify", action="store_true",
                    help="count every article found on tag pages, whatever its own tags say")
    ap.add_argument("--discover", metavar="URL", help="diagnose one article and exit")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    if args.discover:
        discover(session, args.discover)
        return

    cache = load_cache(args.cache)
    if not args.analyze_only:
        print(f"Collecting from {args.start} ({sum(k.startswith('http') for k in cache)} articles "
              f"already cached). Ctrl-C is safe; rerun to resume.")
        try:
            crawl_tags(session, args.tags, args.start, cache, args.cache, args.delay, args.max_pages)
            if not args.no_magazine:
                crawl_magazine(session, args.start, args.end, cache, args.cache, args.delay,
                               args.mag_threshold)
        except KeyboardInterrupt:
            save_cache(cache, args.cache)
            print("\nInterrupted; progress saved. Analyzing what we have so far.")
    analyze(cache, args.tags, args.start, args.end, args.out, args.no_verify, args.strict,
            not args.no_magazine, args.mag_threshold)


if __name__ == "__main__":
    main()
