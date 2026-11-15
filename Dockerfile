FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/tickets.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root user + fakelos gia to database
RUN useradd -m -u 1000 botuser && mkdir /data && chown botuser:botuser /data
COPY --chown=botuser:botuser bot.py .
USER botuser

VOLUME /data
CMD ["python", "bot.py"]
