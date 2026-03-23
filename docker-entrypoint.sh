#!/bin/sh
set -e

# Run Alembic migrations if using Postgres backend
if [ "$AWARENESS_BACKEND" = "postgres" ] && [ -n "$AWARENESS_DATABASE_URL" ]; then
    echo "Running database migrations..."
    mcp-awareness-migrate
    python /app/seed_demo.py
fi

# Start the server
exec mcp-awareness
