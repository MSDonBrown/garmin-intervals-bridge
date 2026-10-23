FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home bridge && mkdir -p /data && chown bridge:bridge /data
USER bridge
ENV BRIDGE_DATA_DIR=/data GARMIN_TOKEN_DIR=/data/tokens BRIDGE_IN_CONTAINER=1
# the web page, when BRIDGE_UI_PORT=8765 and BRIDGE_UI_PASSWORD are set
EXPOSE 8765
# No HEALTHCHECK in the image: compose.yaml and the Unraid template set it (`status --check`,
# every 5 minutes).
ENTRYPOINT ["garmin-intervals-bridge"]
# `run`: polls, full runs, the daily health check – a dry run until the command says --apply
CMD ["run"]
