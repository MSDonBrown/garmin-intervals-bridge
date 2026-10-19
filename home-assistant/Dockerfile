# The bridge's own image, as a Home Assistant app: run as root (Home Assistant owns /data),
# settings from the app's form (/data/options.json), the web page through ingress.
FROM futurewebat/garmin-intervals-bridge:0.11.6
USER root
COPY run.sh /run.sh
RUN chmod 755 /run.sh
ENTRYPOINT ["/run.sh"]
CMD []
