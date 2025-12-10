FROM python:3.12-slim

WORKDIR /app

# Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App files
COPY . .

# Zorg dat /config bestaat in de container
RUN mkdir -p /config

ENV WATCHER_CONFIG_PATH=/config/settings.json \
    WATCHER_KEYWORDS_PATH=/config/keywords.json \
    WATCHER_RESULTS_DB_PATH=/config/results.db

EXPOSE 8000

CMD ["python", "app.py"]
