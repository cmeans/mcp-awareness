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

FROM python:3.12-slim

LABEL org.opencontainers.image.title="mcp-awareness"
LABEL org.opencontainers.image.description="MCP server for ambient system awareness"
LABEL org.opencontainers.image.source="https://github.com/cmeans/mcp-awareness"
LABEL org.opencontainers.image.url="https://github.com/cmeans/mcp-awareness"
LABEL org.opencontainers.image.documentation="https://github.com/cmeans/mcp-awareness/blob/main/docs/deployment-guide.md"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL org.opencontainers.image.authors="Chris Means"
LABEL org.opencontainers.image.vendor="Chris Means"

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY alembic.ini ./
COPY alembic/ alembic/
COPY docker-entrypoint.sh ./
COPY seed-demo.sql seed_demo.py ./

RUN uv pip install --system --no-cache . \
    && useradd --system --no-create-home awareness \
    && mkdir -p /app/data \
    && chown -R awareness:awareness /app

ENV AWARENESS_TRANSPORT=streamable-http
ENV AWARENESS_HOST=0.0.0.0
ENV AWARENESS_PORT=8420

EXPOSE 8420

USER awareness

ENTRYPOINT ["./docker-entrypoint.sh"]
