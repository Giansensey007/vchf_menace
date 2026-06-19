FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config/ config/
COPY src/ src/
COPY scripts/ scripts/

ENV PYTHONPATH=/app
ENV DB_PATH=/data/bot.db
ENV DRY_RUN=true

RUN mkdir -p /data

CMD ["python", "-m", "src.main"]
