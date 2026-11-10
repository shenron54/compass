FROM python:3.12-slim
WORKDIR /app
# Dependencies first, so a code change reuses that layer.
COPY pyproject.toml README.md ./
RUN mkdir compass && touch compass/__init__.py && pip install --no-cache-dir . && rm -rf compass
COPY compass ./compass
RUN pip install --no-cache-dir --no-deps . \
 && useradd --uid 10001 --create-home compass \
 && mkdir -p /data /app/config/local && chown compass /data
USER compass
ENV PYTHONUNBUFFERED=1 COMPASS_DATA_DIR=/data COMPASS_LOCAL_CONFIG=/app/config/local
