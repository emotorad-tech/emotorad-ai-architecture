FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY knowledge/ knowledge/
COPY bots/ bots/
COPY docker/start.sh start.sh
RUN chmod +x start.sh

ENV PYTHONPATH=/app/src
ENV EMOTORAD_AI_LOG_PATH=/app/logs/conversations.jsonl
ENV EMOTORAD_AI_LOG_STDOUT=1

EXPOSE 8000

CMD ["./start.sh"]
