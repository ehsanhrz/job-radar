FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tehran

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jobradar ./jobradar
COPY config.yaml .

RUN mkdir -p /app/data

ENTRYPOINT ["python", "-m", "jobradar"]
CMD ["loop"]
