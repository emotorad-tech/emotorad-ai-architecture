FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The DB-IP Lite city file for origin.py, the newest each build. Never fails
# the build; /health says whether it is there.
COPY docker/fetch_geo_db.py /tmp/fetch_geo_db.py
RUN python /tmp/fetch_geo_db.py /app/geo && rm /tmp/fetch_geo_db.py
ENV EMOTORAD_GEO_DB=/app/geo/dbip-city-lite.mmdb

COPY src/ src/
COPY knowledge/ knowledge/
COPY web/ web/
COPY docker/start.py start.py

ENV PYTHONPATH=/app/src
ENV EMOTORAD_AI_LOG_PATH=/app/logs/conversations.jsonl
ENV EMOTORAD_AI_LOG_STDOUT=1

EXPOSE 8000

CMD ["python", "start.py"]
