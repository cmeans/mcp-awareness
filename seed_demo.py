"""Seed demo data on first run if the database is empty."""

import os
import sys

import psycopg


def main() -> None:
    url = os.environ.get("AWARENESS_DATABASE_URL", "")
    if not url:
        return

    seed_path = os.path.join(os.path.dirname(__file__), "seed-demo.sql")
    if not os.path.exists(seed_path):
        seed_path = "/app/seed-demo.sql"
    if not os.path.exists(seed_path):
        return

    conn = psycopg.connect(url)
    try:
        count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]  # type: ignore[index]
        if count == 0:
            print("First run detected — seeding demo data...")
            conn.execute(open(seed_path).read())
            conn.commit()
            print("Demo data loaded. Ask your AI to use the getting-started prompt!")
        else:
            print(f"Database has {count} entries, skipping seed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
