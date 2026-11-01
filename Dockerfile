# Single container: Telegram bot + scheduled scraping in one process.
# Mount a PERSISTENT volume at /app/data, or the DB is wiped on every redeploy.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tehran \
    JOBRADAR_DB=/app/data/jobradar.db \
    JOBRADAR_CONFIG=/app/config.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jobradar ./jobradar
COPY config.yaml .

RUN mkdir -p /app/data
VOLUME ["/app/data"]

# If the platform requires a port, set PORT (e.g. 8080): GET / returns health JSON.
ENTRYPOINT ["python", "-m", "jobradar"]
CMD ["serve"]
