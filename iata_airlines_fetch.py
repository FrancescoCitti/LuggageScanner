import csv
import json
import math
import re

import requests
from bs4 import BeautifulSoup

BASE = 'https://www.iata.org/en/about/members/airline-list/'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; Francesco-IATA-Collector/1.0; +https://example.com)'
}


def get_total_pages(session):
    r = session.get(BASE, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r'Found\s+(\d+)\s+airline members', r.text, re.I)
    total = int(m.group(1)) if m else 376
    pages = math.ceil(total / 10)
    return total, pages


def parse_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table')
    rows = []
    if not table:
        return rows
    trs = table.find_all('tr')
    for tr in trs[1:]:
        cells = tr.find_all(['td', 'th'])
        tds = [td.get_text(' ', strip=True) for td in cells]
        if len(tds) < 5:
            continue

        member_id = ''
        if cells:
            a = cells[0].find('a', href=True)
            if a:
                parts = [p for p in a['href'].split('/') if p]
                # IATA member page URLs end with .../airline-list/<slug>/<id>/
                if len(parts) >= 2 and parts[-1].isdigit():
                    member_id = parts[-1]

        rows.append({
            'airline_name': tds[0],
            'iata_designator': tds[1],
            'three_digit_code': tds[2],
            'icao_code': tds[3],
            'country_territory': tds[4],
            'iata_member_id': member_id,
        })
    return rows


def fetch_all_members():
    session = requests.Session()
    total, pages = get_total_pages(session)
    data = []
    for page in range(1, pages + 1):
        url = BASE if page == 1 else f'{BASE}?page={page}'
        r = session.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data.extend(parse_page(r.text))
    dedup = []
    seen = set()
    for row in data:
        key = (row['airline_name'], row['iata_designator'], row['icao_code'])
        if key not in seen:
            seen.add(key)
            dedup.append(row)
    return total, dedup



def main():
    total, members = fetch_all_members()
    with open('output/iata_members_raw.json', 'w', encoding='utf-8') as f:
        json.dump({'reported_total': total, 'scraped_total': len(members), 'members': members}, f, ensure_ascii=False, indent=2)
    fieldnames = [
        'airline_name', 'iata_designator', 'three_digit_code', 'icao_code',
        'country_territory', 'iata_member_id', 'official_site',
    ]
    with open('output/iata_members.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in members:
            out = dict(row)
            out['official_site'] = ''
            writer.writerow(out)
    has_id = sum(1 for m in members if m.get('iata_member_id'))
    print(f'Saved {len(members)} members ({has_id} with IATA member ID) to output/iata_members.csv and output/iata_members_raw.json')


if __name__ == '__main__':
    main()