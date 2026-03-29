"""F95zone Latest Updates scraper. Dumps all items into a SQLite database."""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE_URL = "https://f95zone.to/sam/latest_alpha"
DB_PATH = Path(__file__).parent / "f95zone.db"
CONFIG_PATH = Path(__file__).parent / "config.json"

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
    ),
}


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def init_db(db: sqlite3.Connection):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prefixes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            css_class TEXT,
            group_name TEXT
        );
        CREATE TABLE IF NOT EXISTS prefix_categories (
            prefix_id INTEGER NOT NULL REFERENCES prefixes(id),
            category TEXT NOT NULL,
            PRIMARY KEY (prefix_id, category)
        );
        CREATE TABLE IF NOT EXISTS items (
            thread_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            creator TEXT,
            version TEXT,
            views INTEGER,
            likes INTEGER,
            rating REAL,
            cover_url TEXT,
            date_text TEXT,
            timestamp INTEGER,
            category TEXT,
            scraped_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS item_tags (
            thread_id INTEGER NOT NULL REFERENCES items(thread_id),
            tag_id INTEGER NOT NULL REFERENCES tags(id),
            PRIMARY KEY (thread_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS item_prefixes (
            thread_id INTEGER NOT NULL REFERENCES items(thread_id),
            prefix_id INTEGER NOT NULL REFERENCES prefixes(id),
            PRIMARY KEY (thread_id, prefix_id)
        );
        CREATE TABLE IF NOT EXISTS item_screens (
            thread_id INTEGER NOT NULL REFERENCES items(thread_id),
            url TEXT NOT NULL,
            position INTEGER,
            PRIMARY KEY (thread_id, url)
        );
        CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
        CREATE INDEX IF NOT EXISTS idx_items_creator ON items(creator);
        CREATE INDEX IF NOT EXISTS idx_items_rating ON items(rating);
        CREATE INDEX IF NOT EXISTS idx_items_timestamp ON items(timestamp);
        CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id);
    """)


def make_session(config: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(config["cookies"])
    return session


def fetch_tags_and_prefixes(session: requests.Session, db: sqlite3.Connection):
    """Fetch the main page and extract tag/prefix definitions from the embedded JS."""
    print("Fetching tag and prefix definitions...")
    resp = session.get(f"{BASE_URL}/", headers={"accept": "text/html"})
    resp.raise_for_status()

    match = re.search(r"var\s+latestUpdates\s*=\s*({.*?});\s*</script>", resp.text, re.DOTALL)
    if not match:
        print("ERROR: Could not find latestUpdates variable in page HTML")
        sys.exit(1)

    data = json.loads(match.group(1))

    # Insert tags
    tags = data.get("tags", {})
    for tag_id, tag_name in tags.items():
        db.execute(
            "INSERT OR REPLACE INTO tags (id, name) VALUES (?, ?)",
            (int(tag_id), tag_name),
        )
    print(f"  Loaded {len(tags)} tags")

    # Insert prefixes
    prefix_count = 0
    prefixes_by_cat = data.get("prefixes", {})
    for category, groups in prefixes_by_cat.items():
        for group in groups:
            group_name = group["name"]
            for prefix in group["prefixes"]:
                db.execute(
                    "INSERT OR REPLACE INTO prefixes (id, name, css_class, group_name) "
                    "VALUES (?, ?, ?, ?)",
                    (prefix["id"], prefix["name"], prefix.get("class"), group_name),
                )
                db.execute(
                    "INSERT OR IGNORE INTO prefix_categories (prefix_id, category) VALUES (?, ?)",
                    (prefix["id"], category),
                )
                prefix_count += 1
    print(f"  Loaded {prefix_count} prefix entries across categories")
    db.commit()


def fetch_page(session: requests.Session, category: str, page: int, rows: int) -> dict:
    """Fetch a single page of items from the API."""
    resp = session.get(
        f"{BASE_URL}/latest_data.php",
        headers={
            "accept": "application/json, text/javascript, */*; q=0.01",
            "x-requested-with": "XMLHttpRequest",
        },
        params={
            "cmd": "list",
            "cat": category,
            "page": page,
            "sort": "date",
            "rows": rows,
        },
    )
    resp.raise_for_status()
    return resp.json()


def store_items(db: sqlite3.Connection, items: list[dict], category: str):
    """Upsert a batch of items into the database."""
    now = int(time.time())
    for item in items:
        tid = item["thread_id"]
        db.execute(
            """INSERT OR REPLACE INTO items
               (thread_id, title, creator, version, views, likes, rating,
                cover_url, date_text, timestamp, category, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tid,
                item["title"],
                item.get("creator"),
                item.get("version"),
                item.get("views"),
                item.get("likes"),
                item.get("rating"),
                item.get("cover"),
                item.get("date"),
                item.get("ts"),
                category,
                now,
            ),
        )
        # Tags
        db.execute("DELETE FROM item_tags WHERE thread_id = ?", (tid,))
        for tag_id in item.get("tags", []):
            db.execute(
                "INSERT OR IGNORE INTO item_tags (thread_id, tag_id) VALUES (?, ?)",
                (tid, tag_id),
            )
        # Prefixes
        db.execute("DELETE FROM item_prefixes WHERE thread_id = ?", (tid,))
        for prefix_id in item.get("prefixes", []):
            db.execute(
                "INSERT OR IGNORE INTO item_prefixes (thread_id, prefix_id) VALUES (?, ?)",
                (tid, prefix_id),
            )
        # Screenshots
        db.execute("DELETE FROM item_screens WHERE thread_id = ?", (tid,))
        for pos, url in enumerate(item.get("screens", [])):
            db.execute(
                "INSERT OR IGNORE INTO item_screens (thread_id, url, position) VALUES (?, ?, ?)",
                (tid, url, pos),
            )


def scrape_category(session: requests.Session, db: sqlite3.Connection, category: str, config: dict):
    """Scrape all pages for a single category."""
    rows = config.get("rows_per_page", 90)
    delay = config.get("delay_between_requests", 0.5)

    # Get first page to know total
    print(f"\n[{category}] Fetching page 1 to get total count...")
    result = fetch_page(session, category, 1, rows)
    if result["status"] != "ok":
        print(f"  ERROR: API returned status={result['status']}")
        return

    msg = result["msg"]
    total_pages = msg["pagination"]["total"]
    total_count = msg["count"]
    print(f"  {total_count} items across {total_pages} pages (rows={rows})")

    # Store first page
    store_items(db, msg["data"], category)
    db.commit()

    # Fetch remaining pages
    for page in range(2, total_pages + 1):
        progress = f"[{category}] Page {page}/{total_pages}"
        print(f"  {progress}", end="\r")

        try:
            result = fetch_page(session, category, page, rows)
            if result["status"] != "ok":
                print(f"\n  WARN: Page {page} returned status={result['status']}, skipping")
                continue
            store_items(db, result["msg"]["data"], category)
            if page % 10 == 0:
                db.commit()
                items_so_far = page * rows
                print(f"  {progress} — committed (~{min(items_so_far, total_count)}/{total_count} items)")
        except requests.RequestException as e:
            print(f"\n  ERROR on page {page}: {e}")
            print(f"  Waiting 5s then retrying...")
            time.sleep(5)
            try:
                result = fetch_page(session, category, page, rows)
                store_items(db, result["msg"]["data"], category)
            except Exception as e2:
                print(f"  RETRY FAILED: {e2}, skipping page {page}")
                continue

        time.sleep(delay)

    db.commit()
    count = db.execute(
        "SELECT COUNT(*) FROM items WHERE category = ?", (category,)
    ).fetchone()[0]
    print(f"\n  [{category}] Done — {count} items in database")


def print_summary(db: sqlite3.Connection):
    print("\n" + "=" * 50)
    print("DATABASE SUMMARY")
    print("=" * 50)
    for table in ["items", "tags", "prefixes", "prefix_categories", "item_tags", "item_prefixes", "item_screens"]:
        count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")
    print()
    print("Items by category:")
    for row in db.execute("SELECT category, COUNT(*) FROM items GROUP BY category ORDER BY COUNT(*) DESC"):
        print(f"  {row[0]}: {row[1]}")
    print()
    print("Top 10 tags by usage:")
    for row in db.execute("""
        SELECT t.name, COUNT(*) as cnt
        FROM item_tags it JOIN tags t ON it.tag_id = t.id
        GROUP BY t.id ORDER BY cnt DESC LIMIT 10
    """):
        print(f"  {row[0]}: {row[1]}")


def main():
    parser = argparse.ArgumentParser(description="F95zone Latest Updates scraper")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
    parser.add_argument("--categories", nargs="*", help="Categories to scrape (default: from config)")
    parser.add_argument("--tags-only", action="store_true", help="Only fetch tags/prefixes, skip items")
    parser.add_argument("--summary", action="store_true", help="Print DB summary and exit")
    args = parser.parse_args()

    config = load_config()
    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    init_db(db)

    if args.summary:
        print_summary(db)
        db.close()
        return

    session = make_session(config)

    # Always refresh tags/prefixes
    fetch_tags_and_prefixes(session, db)

    if args.tags_only:
        print_summary(db)
        db.close()
        return

    categories = args.categories or config.get("categories", ["games", "comics", "animations", "assets"])
    for cat in categories:
        scrape_category(session, db, cat, config)

    print_summary(db)
    db.close()
    print("\nDone! Database saved to:", args.db)


if __name__ == "__main__":
    main()
