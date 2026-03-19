FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

ENV AWARENESS_DATA_DIR=/app/data
ENV AWARENESS_TRANSPORT=streamable-http
ENV AWARENESS_HOST=0.0.0.0
ENV AWARENESS_PORT=8420
VOLUME /app/data

EXPOSE 8420

ENTRYPOINT ["mcp-awareness"]
