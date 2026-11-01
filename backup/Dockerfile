# InfluxDB backup runner: snapshot or export, written to several paths.
#
# The influx CLI ships with the server image, so no extra tooling is needed.
FROM influxdb:2.8

COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

# numeric uid: the CronJob runs non-root, the mounted volumes must allow it
USER 10001:10001

ENTRYPOINT ["/usr/local/bin/backup.sh"]
