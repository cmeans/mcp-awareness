FROM python:3.12-slim

LABEL org.opencontainers.image.title="mcp-awareness"
LABEL org.opencontainers.image.description="MCP server for ambient system awareness"
LABEL org.opencontainers.image.source="https://github.com/cmeans/mcp-awareness"
LABEL org.opencontainers.image.url="https://github.com/cmeans/mcp-awareness"
LABEL org.opencontainers.image.documentation="https://github.com/cmeans/mcp-awareness/blob/main/docs/deployment-guide.md"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="Chris Means"
LABEL org.opencontainers.image.vendor="Chris Means"

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY alembic.ini ./
COPY alembic/ alembic/
COPY docker-entrypoint.sh ./
COPY seed-demo.sql seed_demo.py ./

RUN pip install --no-cache-dir . \
    && useradd --system --no-create-home awareness \
    && mkdir -p /app/data \
    && chown -R awareness:awareness /app

ENV AWARENESS_TRANSPORT=streamable-http
ENV AWARENESS_HOST=0.0.0.0
ENV AWARENESS_PORT=8420

EXPOSE 8420

USER awareness

ENTRYPOINT ["./docker-entrypoint.sh"]
