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
# Still working? A run within twice the expected time – no request to anyone (`status --check`).
# A break Intervals asked for, or a Garmin sign-in it waits for, count as healthy.
HEALTHCHECK --interval=5m --timeout=30s --start-period=2m --retries=3 \
    CMD ["garmin-intervals-bridge", "status", "--check"]
ENTRYPOINT ["garmin-intervals-bridge"]
# `run`: polls, full runs, the daily health check – a dry run until the command says --apply
CMD ["run"]
