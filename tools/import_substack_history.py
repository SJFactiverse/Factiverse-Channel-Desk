"""
One-time backfill: import Substack's own CSV exports into the Channel Desk database.

Run this ONCE per export. Safe to re-run — it won't create duplicate rows for the
same date (it replaces that day's entry instead).

USAGE:
    1. Install dependency if you don't already have it:
         pip install psycopg2-binary
    2. Set your Neon connection string (same one used in Render's Environment tab):
         Windows (Command Prompt):  set DATABASE_URL=postgresql://...
         Mac/Linux:                 export DATABASE_URL=postgresql://...
    3. Run:
         python import_substack_history.py followers.csv email_stats.csv

Both CSVs are optional — pass just one if that's all you have. Substack names them
something like factiverseai_followers_<date>.csv and factiverseai_email_stats_<date>.csv.
"""

import csv
import json
import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def parse_followers(path):
    """Returns {date_str: subscriber_count}"""
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or not row[0].strip():
                continue
            date_str = row[0].strip().replace("/", "-")
            try:
                count = int(row[1])
            except (ValueError, IndexError):
                continue
            out[date_str] = count
    return out


def parse_email_stats(path):
    """Returns {date_str: {open_rate, reads, title}} — takes the post with the fullest data per day."""
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            post_date = row.get("post_date", "")
            if not post_date:
                continue
            date_str = post_date[:10]  # YYYY-MM-DD
            open_rate = row.get("open_rate")
            views = row.get("views")
            title = row.get("title")
            entry = {}
            if open_rate:
                try:
                    entry["open_rate"] = round(float(open_rate) * 100, 2)
                except ValueError:
                    pass
            if views:
                try:
                    entry["reads"] = int(views)
                except ValueError:
                    pass
            if title:
                entry["title"] = title
            if entry:
                out[date_str] = entry
    return out


def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set. See the instructions at the top of this file.")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python import_substack_history.py <followers.csv> [email_stats.csv]")
        sys.exit(1)

    followers = {}
    email_stats = {}

    for path in sys.argv[1:]:
        if "follower" in path.lower():
            followers = parse_followers(path)
            print(f"Read {len(followers)} days of follower counts from {path}")
        elif "email_stat" in path.lower():
            email_stats = parse_email_stats(path)
            print(f"Read {len(email_stats)} days of post stats from {path}")
        else:
            print(f"Skipping {path} — couldn't tell if it's a followers or email_stats export.")

    all_dates = set(followers) | set(email_stats)
    if not all_dates:
        print("No usable rows found. Nothing imported.")
        return

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()

    inserted, updated = 0, 0
    for date_str in sorted(all_dates):
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        data = {}
        if date_str in followers:
            data["subscribers"] = followers[date_str]
        if date_str in email_stats:
            data.update(email_stats[date_str])

        cur.execute(
            "SELECT id FROM manual_entries WHERE platform = 'substack' AND entry_date = %s;",
            (date_str,)
        )
        existing = cur.fetchone()

        if existing:
            cur.execute(
                "UPDATE manual_entries SET data = data || %s::jsonb WHERE id = %s;",
                (json.dumps(data), existing[0])
            )
            updated += 1
        else:
            cur.execute(
                "INSERT INTO manual_entries (platform, entry_date, data) VALUES ('substack', %s, %s);",
                (date_str, json.dumps(data))
            )
            inserted += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"Done. Inserted {inserted} new days, updated {updated} existing days.")
    print("Refresh the dashboard to see the trend line.")


if __name__ == "__main__":
    main()
