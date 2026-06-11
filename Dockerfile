FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

# Install tzdata so /etc/localtime reflects $TZ (slim images skip it).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Fly's volume will be mounted at /data; the app reads GK_DB from env.
CMD ["gunicorn", "--workers", "1", "--threads", "2", "--bind", "0.0.0.0:8080", "app:app"]
