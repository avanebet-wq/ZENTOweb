FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY . .

# База хранится на томе Railway в /data (чтобы не терялась при перезапуске)
CMD ["sh", "-c", "python3 server/app.py --host 0.0.0.0 --port ${PORT:-8080} --db /data/store.db"]
