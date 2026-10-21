FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements-prod.txt ./
RUN pip install -r requirements-prod.txt

COPY src ./src
COPY web ./web

# The database and uploaded files live in /data. Mount a persistent volume there, or every
# redeploy starts with an empty database.
RUN useradd --system --create-home app && mkdir -p /data && chown app /data
USER app
ENV WHYMOVED_DB=/data/whymoved.db WHYMOVED_DATA_DIR=/data/uploads PORT=8000
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/health', timeout=3)"

CMD ["sh", "-c", "exec uvicorn whymoved.api:create_app --factory --app-dir src --host 0.0.0.0 --port ${PORT}"]
