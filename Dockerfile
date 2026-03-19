FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

ENV AWARENESS_DATA_DIR=/app/data
VOLUME /app/data

EXPOSE 8420

ENTRYPOINT ["mcp-awareness"]
