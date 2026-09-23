"""
One-time backfill: import LinkedIn's own analytics exports into the Channel Desk database.

LinkedIn Page admin -> Analytics gives you two exports that matter here:
  - Content export (the .xls with a "Metrics" tab)   -> daily impressions and reactions
  - Followers export (the .xls with a "New followers" tab) -> daily new-follower counts

Run this ONCE per pair of exports. Safe to re-run — it updates each day's row rather
than duplicating it.

USAGE:
    1. Install dependencies if you don't already have them:
         pip install psycopg2-binary pandas xlrd
    2. Set your Neon connection string (same one used in Render's Environment tab):
         Windows (Command Prompt):  set DATABASE_URL=postgresql://...
         Mac/Linux:                 export DATABASE_URL=postgresql://...
    3. Run:
         python import_linkedin_history.py factiverse_content_XXXX.xls factiverse_followers_XXXX.xls

Both files are optional — pass just one if that's all you have.
"""

import json
import os
import sys
from datetime import datetime

import pandas as pd
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def parse_content(path):
    """Returns {date_str: {impressions, reactions}} from the 'Metrics' tab."""
    df = pd.read_excel(path, sheet_name="Metrics", header=1)
    out = {}
    for _, row in df.iterrows():
        date_raw = row.get("Date")
        if pd.isna(date_raw):
            continue
        try:
            date_str = datetime.strptime(str(date_raw), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        entry = {}
        if "Impressions (total)" in df.columns and pd.notna(row["Impressions (total)"]):
            entry["impressions"] = int(row["Impressions (total)"])
        if "Reactions (total)" in df.columns and pd.notna(row["Reactions (total)"]):
            entry["reactions"] = int(row["Reactions (total)"])
        if entry:
            out[date_str] = entry
    return out


def parse_followers(path):
    """Returns {date_str: {new_followers}} from the 'New followers' tab."""
    df = pd.read_excel(path, sheet_name="New followers")
    out = {}
    for _, row in df.iterrows():
        date_raw = row.get("Date")
        if pd.isna(date_raw):
            continue
        try:
            date_str = datetime.strptime(str(date_raw), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        if "Total followers" in df.columns and pd.notna(row["Total followers"]):
            out[date_str] = {"new_followers": int(row["Total followers"])}
    return out


def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set. See the instructions at the top of this file.")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python import_linkedin_history.py <content.xls> [followers.xls]")
        sys.exit(1)

    content = {}
    followers = {}

    for path in sys.argv[1:]:
        try:
            xl = pd.ExcelFile(path)
        except Exception as e:
            print(f"Couldn't open {path}: {e}")
            continue
        if "Metrics" in xl.sheet_names:
            content = parse_content(path)
            print(f"Read {len(content)} days of impressions/reactions from {path}")
        elif "New followers" in xl.sheet_names:
            followers = parse_followers(path)
            print(f"Read {len(followers)} days of new-follower counts from {path}")
        else:
            print(f"Skipping {path} — didn't recognise its sheet names: {xl.sheet_names}")

    all_dates = set(content) | set(followers)
    if not all_dates:
        print("No usable rows found. Nothing imported.")
        return

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()

    inserted, updated = 0, 0
    for date_str in sorted(all_dates):
        data = {}
        if date_str in content:
            data.update(content[date_str])
        if date_str in followers:
            data.update(followers[date_str])

        cur.execute(
            "SELECT id FROM manual_entries WHERE platform = 'linkedin' AND entry_date = %s;",
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
                "INSERT INTO manual_entries (platform, entry_date, data) VALUES ('linkedin', %s, %s);",
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
