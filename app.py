import os
import json
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import urllib.request
import urllib.error
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
