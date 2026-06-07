FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so this layer is cached on code-only changes
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" pydantic pydantic-settings

# Copy application code
COPY usagi_search/ usagi_search/

# Non-root user
RUN useradd -m appuser
USER appuser

EXPOSE 8000

# The search DB is large (5-6 GB) and built separately — mount it as a volume.
# Set USAGI_CONCEPT_DB_PATH to the mounted path at runtime.
#
# Example:
#   docker run -p 8000:8000 \
#     -v /data/search.db:/data/search.db:ro \
#     -e USAGI_CONCEPT_DB_PATH=/data/search.db \
#     usagi-search:latest

ENV USAGI_USAGI_DIR=/data
ENV USAGI_CONCEPT_DB_PATH=/data/search.db

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "usagi_search.api:app", "--host", "0.0.0.0", "--port", "8000"]
