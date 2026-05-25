import argparse
import csv
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

OUTPUT_CSV = "output/iata_members.csv"
OUTPUT_JSON = "output/iata_members_raw.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LuggageScanner/1.2; +https://example.com)"
}

REQUEST_TIMEOUT = 25

# Third-party aggregators and non-airline domains that should never be accepted
# as an airline's official site.
BLOCKED_DOMAINS = {
    "alternativeairlines.com",
    "unisco.com",
    "zbordirect.com",
    "airlineinformation.com",
    "ch-aviation.com",
    "planespotters.net",
    "flightaware.com",
    "flightradar24.com",
    "airportia.com",
    "kayak.com",
    "skyscanner.com",
    "skyscanner.net",
    "expedia.com",
    "booking.com",
    "tripadvisor.com",
    "wikipedia.org",
    "wikidata.org",
    "uptodown.com",
    "apkpure.com",
    "google.com",
    "bing.com",
}

SITE_STATUS_FIELDS = [
    "official_site",
    "site_status",
    "site_source",
    "site_checked_at",
    "site_notes",
]


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_whitespace(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_url(value):
    value = normalize_whitespace(value)
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://{value}"


def slugify(value):
    value = normalize_whitespace(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value


def fetch_html(session, url):
    response = session.get(
        url,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def is_blocked_domain(url):
    if not url:
        return True
    try:
        netloc = re.sub(r"^www\.", "", urlparse(url).netloc.lower())
        return any(netloc == d or netloc.endswith("." + d) for d in BLOCKED_DOMAINS)
    except Exception:
        return True


def extract_website_from_iata_page(html):
    soup = BeautifulSoup(html, "html.parser")

    # Pattern 1: dt/dd or label+sibling containing "website"
    for tag in soup.find_all(["dt", "th", "label", "strong"]):
        if "website" not in tag.get_text(" ", strip=True).lower():
            continue
        sibling = tag.find_next_sibling()
        if sibling:
            a = sibling.find("a", href=True)
            if a and a["href"].startswith("http"):
                return normalize_url(a["href"])
            text = sibling.get_text(" ", strip=True)
            if text:
                return normalize_url(text)

    # Pattern 2: any paragraph/list-item that looks like "Website: <url>"
    text_block = soup.get_text("\n", strip=True)
    m = re.search(
        r"(?:Website|Web\s*site)\s*[:\-]?\s*(https?://[^\s]+|[A-Za-z0-9][A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?)",
        text_block,
        re.IGNORECASE,
    )
    if m:
        return normalize_url(m.group(1))

    # Pattern 3: <a> whose visible text looks like a domain (contains "www." or ends in a TLD)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            continue
        label = a.get_text(" ", strip=True)
        if re.match(r"(?:www\.)?[A-Za-z0-9-]+\.[A-Za-z]{2,}", label):
            return normalize_url(href)

    return ""


def build_iata_url(row):
    airline_name = row.get("airline_name", "")
    airline_id = normalize_whitespace(row.get("id", "")) or normalize_whitespace(row.get("iata_member_id", ""))
    slug = slugify(airline_name)

    if airline_id:
        return f"https://www.iata.org/en/about/members/airline-list/{slug}/{airline_id}/"

    return ""


def resolve_site_from_iata(session, row):
    iata_url = build_iata_url(row)

    if not iata_url:
        return {
            "official_site": "",
            "site_status": "manual_review",
            "site_source": "iata_member_page",
            "site_checked_at": now_iso(),
            "site_notes": "missing_iata_member_id",
        }

    try:
        html = fetch_html(session, iata_url)
    except Exception:
        return {
            "official_site": "",
            "site_status": "manual_review",
            "site_source": "iata_member_page",
            "site_checked_at": now_iso(),
            "site_notes": f"fetch_failed:{iata_url}",
        }

    website = extract_website_from_iata_page(html)

    if website and not is_blocked_domain(website):
        return {
            "official_site": website,
            "site_status": "resolved",
            "site_source": "iata_member_page",
            "site_checked_at": now_iso(),
            "site_notes": iata_url,
        }

    if website:
        return {
            "official_site": "",
            "site_status": "manual_review",
            "site_source": "iata_member_page",
            "site_checked_at": now_iso(),
            "site_notes": f"blocked_domain:{website}",
        }

    return {
        "official_site": "",
        "site_status": "manual_review",
        "site_source": "iata_member_page",
        "site_checked_at": now_iso(),
        "site_notes": f"website_not_found:{iata_url}",
    }


def ensure_columns(rows):
    for row in rows:
        for field in SITE_STATUS_FIELDS:
            if field not in row:
                row[field] = ""
    return rows


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_csv(path, rows):
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_json_members(data, csv_rows):
    lookup = {
        (
            row.get("airline_name", ""),
            row.get("iata_designator", ""),
            row.get("icao_code", ""),
        ): row
        for row in csv_rows
    }

    for member in data.get("members", []):
        key = (
            member.get("airline_name", ""),
            member.get("iata_designator", ""),
            member.get("icao_code", ""),
        )
        row = lookup.get(key)
        if not row:
            continue

        for field in SITE_STATUS_FIELDS:
            member[field] = row.get(field, "")

    return data


def parse_args():
    parser = argparse.ArgumentParser(description="Resolve official airline sites from IATA member pages.")
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def main():
    args = parse_args()

    if not os.path.exists(OUTPUT_CSV):
        raise FileNotFoundError(f"Missing file: {OUTPUT_CSV}")
    if not os.path.exists(OUTPUT_JSON):
        raise FileNotFoundError(f"Missing file: {OUTPUT_JSON}")

    session = requests.Session()
    rows = ensure_columns(load_csv(OUTPUT_CSV))

    start = max(args.start_index, 1)
    end = len(rows) if args.limit <= 0 else min(len(rows), start - 1 + args.limit)

    print(f"Processing rows {start}-{end} of {len(rows)}...", flush=True)

    for idx in range(start - 1, end):
        row = rows[idx]
        result = resolve_site_from_iata(session, row)

        row["official_site"] = result["official_site"]
        row["site_status"] = result["site_status"]
        row["site_source"] = result["site_source"]
        row["site_checked_at"] = result["site_checked_at"]
        row["site_notes"] = result["site_notes"]

        print(
            f"[{idx + 1}/{len(rows)}] {row.get('airline_name')} -> {row.get('official_site') or '-'}",
            flush=True,
        )

        save_csv(OUTPUT_CSV, rows)
        json_data = load_json(OUTPUT_JSON)
        json_data = update_json_members(json_data, rows)
        save_json(OUTPUT_JSON, json_data)

    print("Done.", flush=True)


if __name__ == "__main__":
    main()