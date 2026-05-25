import argparse
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

INPUT_CSV = "output/iata_members.csv"
OUTPUT_DIR = Path("output/airline_rules")

REQUEST_TIMEOUT_MS = 30_000   # 30 s per navigation
JS_SETTLE_MS = 2_000          # wait after load for JS frameworks to render
REQUEST_DELAY_SECONDS = 1.0

CANDIDATE_KEYWORDS = {
    "baggage": [
        "baggage", "baggage allowance", "carry-on", "carry on",
        "cabin baggage", "hand baggage", "checked baggage"
    ],
    "restricted": [
        "restricted items", "prohibited items", "dangerous goods",
        "forbidden items", "banned items", "what can i bring"
    ],
    "battery": [
        "lithium battery", "lithium batteries", "power bank",
        "spare battery", "electronic cigarette", "e-cigarette",
        "smart bag", "lighter", "matches"
    ],
}

BLOCKED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip"
}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_ymd():
    return datetime.now().strftime("%Y-%m-%d")


def slugify(value):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "unknown_airline"


def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_url(url):
    if not url:
        return ""
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    path = parsed.path or "/"
    return f"{scheme}://{netloc}{path}"


def same_domain(base_url, candidate_url):
    try:
        base_host = urlparse(base_url).netloc.lower()
        candidate_host = urlparse(candidate_url).netloc.lower()
        return base_host == candidate_host
    except Exception:
        return False


def has_blocked_extension(url):
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in BLOCKED_EXTENSIONS)


def fetch_page(pw_page, url):
    """Navigate to URL with Chromium and return (html, final_url).
    Raises on HTTP errors or navigation timeout."""
    response = pw_page.goto(url, timeout=REQUEST_TIMEOUT_MS, wait_until="load")
    if response is None:
        raise Exception("no response")
    if not response.ok:
        raise Exception(f"HTTP {response.status}")
    pw_page.wait_for_timeout(JS_SETTLE_MS)
    return pw_page.content(), pw_page.url


def extract_links(base_url, html):
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = normalize_text(a.get_text(" ", strip=True))
        if not href:
            continue

        absolute = urljoin(base_url, href)
        absolute = normalize_url(absolute)

        if not absolute.startswith("http"):
            continue
        if not same_domain(base_url, absolute):
            continue
        if has_blocked_extension(absolute):
            continue

        links.append({"url": absolute, "text": text})

    dedup = {}
    for link in links:
        dedup[link["url"]] = link
    return list(dedup.values())


def score_link(text, url, keywords):
    text_l = (text or "").lower()
    url_l = (url or "").lower()
    score = 0

    for kw in keywords:
        kw_l = kw.lower()
        if kw_l in text_l:
            score += 4
        if kw_l.replace(" ", "-") in url_l or kw_l.replace(" ", "") in url_l:
            score += 3
        elif kw_l in url_l:
            score += 2

    if "policy" in text_l or "policy" in url_l:
        score += 1
    if "help" in url_l or "support" in url_l or "faq" in url_l:
        score += 1

    return score


def find_best_candidate_links(official_site, homepage_html):
    links = extract_links(official_site, homepage_html)
    buckets = {}

    for bucket_name, keywords in CANDIDATE_KEYWORDS.items():
        scored = []
        for link in links:
            score = score_link(link["text"], link["url"], keywords)
            if score > 0:
                scored.append({"url": link["url"], "text": link["text"], "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        buckets[bucket_name] = scored[:5]

    return buckets


def reset_page(pw_page):
    """Navigate to about:blank to clear any pending navigation or broken state."""
    try:
        pw_page.goto("about:blank", wait_until="commit", timeout=5_000)
    except Exception:
        pass


def fetch_candidate_pages(pw_page, candidates):
    pages = []
    seen = set()

    for candidate in candidates:
        url = candidate["url"]
        if url in seen:
            continue
        seen.add(url)

        try:
            html, final_url = fetch_page(pw_page, url)
            soup = BeautifulSoup(html, "html.parser")
            title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
            body = normalize_text(soup.get_text(" ", strip=True))
            pages.append({
                "url": normalize_url(final_url),
                "title": title,
                "body": body,
                "html": html,
            })
        except Exception:
            reset_page(pw_page)
            continue

        time.sleep(REQUEST_DELAY_SECONDS)

    return pages


def expand_candidates(l1_pages, buckets):
    """Merge sub-links from level-1 fetched pages back into each bucket's candidate list."""
    sub_links = []
    for page in l1_pages:
        sub_links.extend(extract_links(page["url"], page.get("html", "")))

    seen = {}
    for link in sub_links:
        seen[link["url"]] = link
    sub_links = list(seen.values())

    for bucket_name, keywords in CANDIDATE_KEYWORDS.items():
        existing_urls = {c["url"] for c in buckets[bucket_name]}
        new_scored = []
        for link in sub_links:
            if link["url"] in existing_urls:
                continue
            score = score_link(link["text"], link["url"], keywords)
            if score > 0:
                new_scored.append({"url": link["url"], "text": link["text"], "score": score})
        new_scored.sort(key=lambda x: x["score"], reverse=True)

        merged = buckets[bucket_name] + new_scored[:3]
        merged.sort(key=lambda x: x["score"], reverse=True)

        deduped_urls: set = set()
        deduped = []
        for c in merged:
            if c["url"] not in deduped_urls:
                deduped_urls.add(c["url"])
                deduped.append(c)
        buckets[bucket_name] = deduped[:5]

    return buckets


def pick_best_page(pages, keywords):
    best = None
    best_score = 0  # page must have at least one keyword hit to qualify

    for page in pages:
        text = f"{page['title']} {page['body'][:10000]}".lower()
        score = 0
        for kw in keywords:
            score += text.count(kw.lower()) * 2
        if "baggage" in text:
            score += 1
        if score > best_score:
            best_score = score
            best = page

    return best


def extract_carry_on_info(text):
    dim_patterns = [
        r"(\d{2,3})\s*[x×*]\s*(\d{2,3})\s*[x×*]\s*(\d{2,3})\s*cm",
        r"(\d{2,3})\s*cm\s*[x×*]\s*(\d{2,3})\s*cm\s*[x×*]\s*(\d{2,3})\s*cm",
        r"(\d{1,3})\s*[x×*]\s*(\d{1,3})\s*[x×*]\s*(\d{1,3})\s*cm",
    ]

    dims = None
    for p in dim_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            dims = f"{m.group(1)} x {m.group(2)} x {m.group(3)} cm"
            break

    weights = re.findall(r"\b(\d{1,2})\s*kg\b", text, re.IGNORECASE)
    weight = weights[0] + " kg" if weights else None

    return {"dimensions": dims, "weight_limit": weight}


def extract_battery_info(text):
    text_l = text.lower()

    info = {
        "power_bank_max_wh": None,
        "spare_battery_rule": None,
        "power_bank_rule": None,
        "ecig_rule": None,
        "smart_bag_rule": None,
    }

    wh_values = re.findall(r"(\d{2,3})\s*wh", text_l, re.IGNORECASE)
    if wh_values:
        info["power_bank_max_wh"] = sorted({int(v) for v in wh_values})

    if "spare batter" in text_l:
        info["spare_battery_rule"] = "mentioned"
    if "power bank" in text_l:
        info["power_bank_rule"] = "mentioned"
    if "electronic cigarette" in text_l or "e-cigarette" in text_l or "vape" in text_l:
        info["ecig_rule"] = "mentioned"
    if "smart bag" in text_l or "smart baggage" in text_l:
        info["smart_bag_rule"] = "mentioned"

    return info


def extract_lighter_info(text):
    text_l = text.lower()

    if "lighter" not in text_l and "matches" not in text_l:
        return {"lighter_rule": None, "matches_rule": None}

    lighter_rule = None
    matches_rule = None

    lighter_match = re.search(r"[^.]{0,120}(lighter|lighters)[^.]{0,160}\.", text, re.IGNORECASE)
    matches_match = re.search(r"[^.]{0,120}(matches)[^.]{0,160}\.", text, re.IGNORECASE)

    if lighter_match:
        lighter_rule = normalize_text(lighter_match.group(0))
    if matches_match:
        matches_rule = normalize_text(matches_match.group(0))

    return {"lighter_rule": lighter_rule, "matches_rule": matches_rule}


def build_output_payload(row, baggage_page, restricted_page, battery_page):
    baggage_text = baggage_page["body"] if baggage_page else ""
    restricted_text = restricted_page["body"] if restricted_page else ""
    battery_text = battery_page["body"] if battery_page else ""

    combined_text = " ".join([baggage_text, restricted_text, battery_text])

    carry_on = extract_carry_on_info(baggage_text or combined_text)
    battery_info = extract_battery_info(battery_text or restricted_text or combined_text)
    lighter_info = extract_lighter_info(restricted_text or battery_text or combined_text)

    found_baggage_info = bool(carry_on.get("dimensions") or carry_on.get("weight_limit"))
    found_battery_info = bool(
        battery_info["power_bank_max_wh"] or
        battery_info["spare_battery_rule"] or
        battery_info["power_bank_rule"] or
        battery_info["ecig_rule"]
    )
    found_lighter_info = bool(lighter_info["lighter_rule"] or lighter_info["matches_rule"])
    found_restricted_items = bool(
        found_battery_info or found_lighter_info or
        any(
            kw in (restricted_text or "").lower()
            for kw in ["prohibited", "forbidden", "restricted", "not allowed", "not permitted"]
        )
    )

    return {
        "airline_name": row.get("airline_name", ""),
        "iata_designator": row.get("iata_designator", ""),
        "icao_code": row.get("icao_code", ""),
        "country_territory": row.get("country_territory", ""),
        "official_site": row.get("official_site", ""),
        "checked_at": now_iso(),
        "status": "ok" if any([found_baggage_info, found_restricted_items, found_battery_info, found_lighter_info]) else "not_found",
        "baggage_policy_url": baggage_page["url"] if baggage_page else "",
        "restricted_items_url": restricted_page["url"] if restricted_page else "",
        "battery_policy_url": battery_page["url"] if battery_page else "",
        "found_baggage_info": found_baggage_info,
        "found_restricted_items": found_restricted_items,
        "found_battery_info": found_battery_info,
        "found_lighter_info": found_lighter_info,
        "carry_on": carry_on,
        "battery_info": battery_info,
        "lighter_info": lighter_info,
        "prohibited_items_summary": normalize_text((restricted_text or battery_text)[:1200]),
        "notes": "",
    }


def save_airline_json(payload):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    file_path = OUTPUT_DIR / f"{slugify(payload['airline_name'])}.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return file_path


def load_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch airline baggage policy info using a headless Chromium browser.")
    parser.add_argument("--start-index", type=int, default=1, help="1-based row index to start from.")
    parser.add_argument("--limit", type=int, default=0, help="How many rows to process. 0 means all.")
    parser.add_argument("--log-file", type=str, default="", help="Path to write log output. Defaults to output/run_<timestamp>.log.")
    return parser.parse_args()


class Tee:
    """Writes to stdout and a log file simultaneously."""
    def __init__(self, log_path):
        import sys
        self._stdout = sys.stdout
        self._file = open(log_path, "w", encoding="utf-8", buffering=1)

    def write(self, data):
        self._stdout.write(data)
        self._file.write(data)

    def flush(self):
        self._stdout.flush()
        self._file.flush()

    def close(self):
        self._file.close()


def main():
    import sys
    args = parse_args()
    rows = load_csv_rows(INPUT_CSV)

    log_path = args.log_file or f"output/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    tee = Tee(log_path)
    sys.stdout = tee
    print(f"Log: {log_path}")

    start = max(args.start_index, 1)
    end = len(rows) if args.limit <= 0 else min(len(rows), start - 1 + args.limit)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        for idx in range(start - 1, end):
            row = rows[idx]
            airline_name = row.get("airline_name", "").strip()
            official_site = row.get("official_site", "").strip()

            if not official_site:
                print(f"[{idx + 1}/{len(rows)}] {airline_name} -> SKIP -> no official_site")
                continue

            # Fresh page per airline — isolates redirect/crash state between airlines
            pw_page = context.new_page()
            try:
                homepage_html, homepage_final_url = fetch_page(pw_page, official_site)
                homepage_url = normalize_url(homepage_final_url)
            except Exception as e:
                print(f"[{idx + 1}/{len(rows)}] {airline_name} -> ERROR -> {e}")
                pw_page.close()
                continue

            # Level 1: score links found on the rendered homepage
            candidate_buckets = find_best_candidate_links(homepage_url, homepage_html)

            l1_seen: set = set()
            l1_candidates = []
            for candidates in candidate_buckets.values():
                for c in candidates[:3]:
                    if c["url"] not in l1_seen:
                        l1_candidates.append(c)
                        l1_seen.add(c["url"])

            l1_pages = fetch_candidate_pages(pw_page, l1_candidates)

            # Level 2: extract sub-links from level-1 pages and re-score
            candidate_buckets = expand_candidates(l1_pages, candidate_buckets)

            l1_urls = {p["url"] for p in l1_pages}
            l2_seen = set(l1_urls)
            l2_candidates = []
            for candidates in candidate_buckets.values():
                for c in candidates[:3]:
                    if c["url"] not in l2_seen:
                        l2_candidates.append(c)
                        l2_seen.add(c["url"])

            l2_pages = fetch_candidate_pages(pw_page, l2_candidates)

            all_pages = {p["url"]: p for p in l1_pages + l2_pages}

            def pages_for(bucket_name, limit=3):
                return [
                    all_pages[c["url"]]
                    for c in candidate_buckets[bucket_name][:limit]
                    if c["url"] in all_pages
                ]

            baggage_page = pick_best_page(pages_for("baggage"), CANDIDATE_KEYWORDS["baggage"])
            restricted_page = pick_best_page(pages_for("restricted"), CANDIDATE_KEYWORDS["restricted"])
            battery_page = pick_best_page(pages_for("battery"), CANDIDATE_KEYWORDS["battery"])

            payload = build_output_payload(row, baggage_page, restricted_page, battery_page)
            output_path = save_airline_json(payload)

            print(f"[{idx + 1}/{len(rows)}] {airline_name}")
            print(f"  baggage_info: {'FOUND' if payload['found_baggage_info'] else 'NOT FOUND'}")
            print(f"  restricted_items: {'FOUND' if payload['found_restricted_items'] else 'NOT FOUND'}")
            print(f"  battery_info: {'FOUND' if payload['found_battery_info'] else 'NOT FOUND'}")
            print(f"  lighter_info: {'FOUND' if payload['found_lighter_info'] else 'NOT FOUND'}")
            print(f"  saved_to: {output_path}")

            pw_page.close()
            time.sleep(REQUEST_DELAY_SECONDS)

        browser.close()

    tee.close()
    sys.stdout = tee._stdout


if __name__ == "__main__":
    main()
