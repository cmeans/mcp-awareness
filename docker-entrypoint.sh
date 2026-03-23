#!/bin/sh
set -e

# Run Alembic migrations and seed demo data
echo "Running database migrations..."
mcp-awareness-migrate
python /app/seed_demo.py

# Start the server
exec mcp-awareness
