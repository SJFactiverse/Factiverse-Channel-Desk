import os
import json
import csv
import tempfile
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import urllib.request
import urllib.error
import pandas as pd
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")

PLATFORMS = ("x", "linkedin", "substack")


def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS youtube_cache (
                    id SERIAL PRIMARY KEY,
                    channel_name TEXT,
                    subscriber_count BIGINT,
                    view_count BIGINT,
                    video_count BIGINT,
                    fetched_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS manual_entries (
                    id SERIAL PRIMARY KEY,
                    platform TEXT NOT NULL,
                    entry_date DATE,
                    data JSONB NOT NULL,
                    logged_at TIMESTAMPTZ DEFAULT now()
                );
            """)
        conn.commit()


def fetch_youtube_stats():
    if not YOUTUBE_API_KEY or not YOUTUBE_CHANNEL_ID:
        return {"error": "YouTube API key or channel ID isn't set. Add them in Render's Environment settings."}

    url = (
        "https://www.googleapis.com/youtube/v3/channels"
        f"?part=statistics,snippet&id={YOUTUBE_CHANNEL_ID}&key={YOUTUBE_API_KEY}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"YouTube API error: {e.code} {e.reason}"}
    except Exception as e:
        return {"error": f"Could not reach YouTube API: {e}"}

    items = payload.get("items")
    if not items:
        return {"error": "No channel found. Check YOUTUBE_CHANNEL_ID."}

    stats = items[0]["statistics"]
    snippet = items[0]["snippet"]
    return {
        "channel_name": snippet.get("title"),
        "subscriber_count": int(stats.get("subscriberCount", 0)),
        "view_count": int(stats.get("viewCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
    }


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Export parsing — same logic as the tools/import_*.py scripts, so uploading
# a file in the browser does exactly what running the script locally does.
# ---------------------------------------------------------------------------

def upsert_manual(cur, platform, entry_date, data, replace=False):
    cur.execute(
        "SELECT id FROM manual_entries WHERE platform = %s AND entry_date = %s;",
        (platform, entry_date)
    )
    existing = cur.fetchone()
    if existing:
        if replace:
            cur.execute(
                "UPDATE manual_entries SET data = %s::jsonb WHERE id = %s;",
                (json.dumps(data), existing[0])
            )
        else:
            cur.execute(
                "UPDATE manual_entries SET data = data || %s::jsonb WHERE id = %s;",
                (json.dumps(data), existing[0])
            )
        return "updated"
    else:
        cur.execute(
            "INSERT INTO manual_entries (platform, entry_date, data) VALUES (%s, %s, %s);",
            (platform, entry_date, json.dumps(data))
        )
        return "inserted"


def parse_substack_followers(path):
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            date_str = row[0].strip().replace("/", "-")
            try:
                count = int(row[1])
            except (ValueError, IndexError):
                continue
            out[date_str] = count
    return out


def parse_substack_email_stats(path):
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            post_date = row.get("post_date", "")
            if not post_date:
                continue
            date_str = post_date[:10]
            entry = {}
            open_rate = row.get("open_rate")
            views = row.get("views")
            title = row.get("title")
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


def parse_linkedin_content(path):
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


def parse_linkedin_followers(path):
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


def parse_youtube_totals(path):
    df = pd.read_csv(path)
    out = {}
    for _, row in df.iterrows():
        date_raw = row.get("Date")
        views = row.get("Views")
        if pd.isna(date_raw) or pd.isna(views):
            continue
        try:
            date_str = datetime.strptime(str(date_raw), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
        out[date_str] = int(views)
    return out


def parse_youtube_top_videos(path, top_n=10):
    df = pd.read_csv(path)
    df = df[df["Content"] != "Total"]
    df = df.sort_values("Views", ascending=False).head(top_n)
    videos = []
    for _, row in df.iterrows():
        ctr = row.get("Thumbnail click-through rate (%)")
        videos.append({
            "content": row.get("Content"),
            "title": row.get("Video title") if pd.notna(row.get("Video title")) else row.get("Content"),
            "views": int(row["Views"]) if pd.notna(row.get("Views")) else 0,
            "watch_time_hours": float(row["Watch time (hours)"]) if pd.notna(row.get("Watch time (hours)")) else 0,
            "subscribers_gained": int(row["Subscribers"]) if pd.notna(row.get("Subscribers")) else 0,
            "ctr": float(ctr) if pd.notna(ctr) else None
        })
    return videos


def save_uploads(files):
    """Saves each uploaded file to a temp path, returns [(original_filename, temp_path), ...]"""
    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        suffix = os.path.splitext(f.filename)[1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        f.save(tmp.name)
        saved.append((f.filename, tmp.name))
    return saved


@app.route("/api/upload/substack", methods=["POST"])
def upload_substack():
    files = request.files.getlist("files")
    saved = save_uploads(files)
    if not saved:
        return jsonify({"error": "No files received."}), 400

    daily_data = {}
    days_seen = 0
    try:
        for name, path in saved:
            lower = name.lower()
            if "follower" in lower:
                followers = parse_substack_followers(path)
                for date_str, count in followers.items():
                    daily_data.setdefault(date_str, {})["subscribers"] = count
                days_seen += len(followers)
            elif "email_stat" in lower:
                stats = parse_substack_email_stats(path)
                for date_str, entry in stats.items():
                    daily_data.setdefault(date_str, {}).update(entry)
                days_seen += len(stats)
            else:
                return jsonify({"error": f"Didn't recognise '{name}'. Expected a filename containing 'followers' or 'email_stats'."}), 400
    finally:
        for _, path in saved:
            os.unlink(path)

    if not daily_data:
        return jsonify({"error": "No usable rows found in the file(s)."}), 400

    inserted, updated = 0, 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for date_str, data in daily_data.items():
                result = upsert_manual(cur, "substack", date_str, data)
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1
        conn.commit()

    return jsonify({"ok": True, "inserted": inserted, "updated": updated})


@app.route("/api/upload/linkedin", methods=["POST"])
def upload_linkedin():
    files = request.files.getlist("files")
    saved = save_uploads(files)
    if not saved:
        return jsonify({"error": "No files received."}), 400

    daily_data = {}
    try:
        for name, path in saved:
            try:
                xl = pd.ExcelFile(path)
            except Exception as e:
                return jsonify({"error": f"Couldn't open '{name}': {e}"}), 400

            if "Metrics" in xl.sheet_names:
                content = parse_linkedin_content(path)
                for date_str, entry in content.items():
                    daily_data.setdefault(date_str, {}).update(entry)
            elif "New followers" in xl.sheet_names:
                followers = parse_linkedin_followers(path)
                for date_str, entry in followers.items():
                    daily_data.setdefault(date_str, {}).update(entry)
            else:
                return jsonify({"error": f"Didn't recognise the sheets in '{name}'. Expected a LinkedIn Content or Followers export."}), 400
    finally:
        for _, path in saved:
            os.unlink(path)

    if not daily_data:
        return jsonify({"error": "No usable rows found in the file(s)."}), 400

    inserted, updated = 0, 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for date_str, data in daily_data.items():
                result = upsert_manual(cur, "linkedin", date_str, data)
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1
        conn.commit()

    return jsonify({"ok": True, "inserted": inserted, "updated": updated})


@app.route("/api/upload/youtube", methods=["POST"])
def upload_youtube():
    files = request.files.getlist("files")
    saved = save_uploads(files)
    if not saved:
        return jsonify({"error": "No files received."}), 400

    daily_views = {}
    top_videos = None
    try:
        for name, path in saved:
            lower = name.lower()
            if "totals" in lower:
                daily_views = parse_youtube_totals(path)
            elif "table_data" in lower or "table data" in lower:
                top_videos = parse_youtube_top_videos(path)
            else:
                return jsonify({"error": f"Didn't recognise '{name}'. Expected a filename containing 'Totals' or 'Table_data'."}), 400
    finally:
        for _, path in saved:
            os.unlink(path)

    if not daily_views and top_videos is None:
        return jsonify({"error": "No usable rows found in the file(s)."}), 400

    inserted, updated = 0, 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for date_str, views in daily_views.items():
                result = upsert_manual(cur, "youtube_views", date_str, {"views": views}, replace=True)
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1
            if top_videos is not None:
                today = datetime.now(timezone.utc).date().isoformat()
                cur.execute(
                    "INSERT INTO manual_entries (platform, entry_date, data) VALUES ('youtube_top_videos', %s, %s);",
                    (today, json.dumps({"videos": top_videos}))
                )
        conn.commit()

    return jsonify({"ok": True, "inserted": inserted, "updated": updated, "top_videos_saved": top_videos is not None})


@app.route("/api/data")
def api_data():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM youtube_cache ORDER BY fetched_at DESC LIMIT 1;")
            youtube = cur.fetchone()

            cur.execute(
                "SELECT channel_name, subscriber_count, view_count, video_count, fetched_at "
                "FROM youtube_cache ORDER BY fetched_at ASC LIMIT 60;"
            )
            youtube_history = cur.fetchall()

            manual = {}
            for platform in PLATFORMS:
                cur.execute(
                    "SELECT id, entry_date, data, logged_at FROM manual_entries "
                    "WHERE platform = %s ORDER BY entry_date DESC, logged_at DESC LIMIT 60;",
                    (platform,)
                )
                manual[platform] = cur.fetchall()

            cur.execute(
                "SELECT entry_date, data FROM manual_entries "
                "WHERE platform = 'substack' AND data ? 'open_rate' "
                "ORDER BY (data->>'open_rate')::float DESC LIMIT 5;"
            )
            substack_top_posts = cur.fetchall()

            cur.execute(
                "SELECT entry_date, data FROM manual_entries "
                "WHERE platform = 'youtube_views' ORDER BY entry_date ASC LIMIT 400;"
            )
            youtube_views_history = cur.fetchall()

            cur.execute(
                "SELECT data FROM manual_entries "
                "WHERE platform = 'youtube_top_videos' ORDER BY entry_date DESC, logged_at DESC LIMIT 1;"
            )
            top_videos_row = cur.fetchone()
            youtube_top_videos = top_videos_row["data"]["videos"] if top_videos_row else None

    return app.response_class(
        response=json.dumps({
            "youtube": youtube,
            "youtube_history": youtube_history,
            "youtube_views_history": youtube_views_history,
            "youtube_top_videos": youtube_top_videos,
            "substack_top_posts": substack_top_posts,
            "manual": manual,
            "youtube_configured": bool(YOUTUBE_API_KEY and YOUTUBE_CHANNEL_ID)
        }, default=str),
        mimetype="application/json"
    )


@app.route("/api/refresh-youtube", methods=["POST"])
def api_refresh_youtube():
    result = fetch_youtube_stats()
    if "error" in result:
        return jsonify(result), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO youtube_cache (channel_name, subscriber_count, view_count, video_count) "
                "VALUES (%s, %s, %s, %s);",
                (result["channel_name"], result["subscriber_count"], result["view_count"], result["video_count"])
            )
        conn.commit()

    return jsonify(result)


@app.route("/api/manual/<platform>", methods=["POST"])
def api_manual_add(platform):
    if platform not in PLATFORMS:
        return jsonify({"error": "Unknown platform"}), 400

    body = request.get_json(force=True) or {}
    entry_date = body.pop("entry_date", None) or datetime.now(timezone.utc).date().isoformat()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO manual_entries (platform, entry_date, data) VALUES (%s, %s, %s) RETURNING id;",
                (platform, entry_date, json.dumps(body))
            )
            new_id = cur.fetchone()[0]
        conn.commit()

    return jsonify({"ok": True, "id": new_id})


@app.route("/api/manual/<platform>/<int:entry_id>", methods=["DELETE"])
def api_manual_delete(platform, entry_id):
    if platform not in PLATFORMS:
        return jsonify({"error": "Unknown platform"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM manual_entries WHERE id = %s AND platform = %s;",
                (entry_id, platform)
            )
        conn.commit()

    return jsonify({"ok": True})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
