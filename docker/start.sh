#!/bin/sh
# Runs both processes this container needs: the playground's own Streamlit
# server (bound to localhost only — api.py reverse-proxies /playground to
# it, so it never needs a port opened in the security group), and the
# FastAPI app that actually receives traffic on the port that's exposed.
set -e

streamlit run src/emotorad_ai/playground.py \
    --server.port 8501 \
    --server.address 127.0.0.1 \
    --server.baseUrlPath playground \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection false \
    --browser.gatherUsageStats false \
    --client.toolbarMode viewer &

exec uvicorn emotorad_ai.api:app --host 0.0.0.0 --port 8000
