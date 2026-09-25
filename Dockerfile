# Daedalus - collector service and web UI (one image, two jobs)
#
# The image brings its own tools: net-snmp belongs in the image, not on the host.
FROM python:3.12-alpine

# tzdata: without the zone files `zoneinfo` knows no time zone besides UTC, and
# every displayed time would be wrong - in exactly the tool that keeps history.
# net-snmp-tools: snmpbulkwalk for switches and firewall.
# iputils-ping, traceroute, nmap, libcap: the actions of the UI (nmap only as a
# connect scan -sT, which needs no privileges). The service runs without root;
# `setcap cap_net_raw` gives exactly these two programs the right to send ICMP,
# not the whole process.
RUN apk add --no-cache net-snmp-tools tzdata iputils-ping traceroute libcap nmap \
 && setcap cap_net_raw+ep "$(readlink -f "$(command -v ping)")" \
 && setcap cap_net_raw+ep "$(readlink -f "$(command -v traceroute)")" \
 && pip install --no-cache-dir "psycopg[binary]==3.3.5" \
        "fastapi==0.118.3" "uvicorn==0.37.0"

WORKDIR /app
COPY daedalus/ ./daedalus/
COPY run.py schema.sql ./
COPY migrations/ ./migrations/

# No root user: the service only reads, it needs no rights on the image.
RUN adduser -D -H daedalus
USER daedalus

# The configuration is mounted to /config/daedalus.toml (see README).
ENV PYTHONUNBUFFERED=1 DAEDALUS_CONFIG=/config/daedalus.toml
EXPOSE 8000
# The default is the collector service; the web UI sets its own command:
#   uvicorn daedalus.web:app --host 0.0.0.0 --port 8000
# No ENTRYPOINT, so both read the same way.
CMD ["python", "run.py", "--service"]
