# mcp-awareness — ambient system awareness for AI agents
# Copyright (C) 2026 Chris Means
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Seed demo data on first run if the database is empty."""

import os

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
