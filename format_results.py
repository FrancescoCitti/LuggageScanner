"""
Reads the JSON output for a given airline and writes a Markdown summary
to $GITHUB_STEP_SUMMARY (and also prints it to stdout).
"""

import json
import os
import re
import sys
from pathlib import Path


def slugify(value):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "unknown_airline"


def badge(found):
    return "✅ Yes" if found else "❌ No"


def val(v, suffix=""):
    return f"{v}{suffix}" if v else "—"


def main():
    if len(sys.argv) < 2:
        print("Usage: format_results.py <airline_name>")
        sys.exit(1)

    airline_name = " ".join(sys.argv[1:])
    slug = slugify(airline_name)
    json_path = Path(f"output/airline_rules/{slug}.json")

    if not json_path.exists():
        print(f"No results file found for: {airline_name} (looked for {json_path})")
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        d = json.load(f)

    carry_on = d.get("carry_on", {})
    battery = d.get("battery_info", {})
    lighter = d.get("lighter_info", {})
    status = d.get("status", "not_found")
    wh = battery.get("power_bank_max_wh")

    lines = [
        f"# ✈️  {d['airline_name']}",
        "",
        f"| | |",
        f"|---|---|",
        f"| **IATA** | `{d.get('iata_designator') or '—'}` |",
        f"| **ICAO** | `{d.get('icao_code') or '—'}` |",
        f"| **Country** | {d.get('country_territory') or '—'} |",
        f"| **Official site** | {d.get('official_site') or '—'} |",
        f"| **Overall status** | {'✅ Data found' if status == 'ok' else '❌ No data extracted'} |",
        f"| **Last checked** | {d.get('checked_at', '—')} |",
        "",
        "---",
        "",
        "## 🧳 Carry-on Baggage",
        "",
        f"| | |",
        f"|---|---|",
        f"| Dimensions | {val(carry_on.get('dimensions'))} |",
        f"| Weight limit | {val(carry_on.get('weight_limit'))} |",
        f"| Source URL | {val(d.get('baggage_policy_url'))} |",
        "",
        "---",
        "",
        "## 🔋 Battery & Electronics",
        "",
        f"| | |",
        f"|---|---|",
        f"| Max power bank (Wh) | {', '.join(str(x) for x in wh) + ' Wh' if wh else '—'} |",
        f"| Spare batteries | {val(battery.get('spare_battery_rule'))} |",
        f"| Power banks | {val(battery.get('power_bank_rule'))} |",
        f"| E-cigarettes / vapes | {val(battery.get('ecig_rule'))} |",
        f"| Smart bags | {val(battery.get('smart_bag_rule'))} |",
        f"| Source URL | {val(d.get('battery_policy_url'))} |",
        "",
        "---",
        "",
        "## 🔥 Lighters & Matches",
        "",
        f"| | |",
        f"|---|---|",
        f"| Lighters | {val(lighter.get('lighter_rule'))} |",
        f"| Matches | {val(lighter.get('matches_rule'))} |",
        f"| Source URL | {val(d.get('restricted_items_url'))} |",
    ]

    prohibited = d.get("prohibited_items_summary", "").strip()
    if prohibited:
        lines += [
            "",
            "---",
            "",
            "## 🚫 Prohibited Items (excerpt)",
            "",
            "```",
            prohibited[:1000],
            "```",
        ]

    lines += [
        "",
        "---",
        "",
        f"*Data scraped automatically — always verify with the airline's official site.*",
    ]

    md = "\n".join(lines)
    print(md)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(md)


if __name__ == "__main__":
    main()
