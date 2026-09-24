# Daedalus — Sammeldienst und Weboberflaeche (ein Abbild, zwei Aufgaben)
#
# Bringt seine Werkzeuge selbst mit: net-snmp gehoert ins Abbild, nicht auf den
# Wirt. Auf den Lab-Knoten ist bewusst nichts davon installiert.
FROM python:3.12-alpine

# tzdata: ohne die Zonendateien kennt `zoneinfo` kein Europe/Berlin, und jede
# Zeitangabe im Protokoll stuende falsch — in genau dem Werkzeug, das Historie
# fuehrt. net-snmp-tools: snmpbulkwalk fuer Switches und Firewall.
# iputils-ping, traceroute, nmap, libcap: die Aktionen der Oberflaeche (nmap nur
# als Verbindungsscan -sT, der keine Sonderrechte braucht). Der Dienst
# laeuft ohne Root; `setcap cap_net_raw` gibt genau diesen beiden Programmen das
# Recht auf ICMP, nicht dem ganzen Prozess.
RUN apk add --no-cache net-snmp-tools tzdata iputils-ping traceroute libcap nmap \
 && setcap cap_net_raw+ep "$(readlink -f "$(command -v ping)")" \
 && setcap cap_net_raw+ep "$(readlink -f "$(command -v traceroute)")" \
 && pip install --no-cache-dir "psycopg[binary]==3.3.5" \
        "fastapi==0.118.3" "uvicorn==0.37.0"

WORKDIR /app
COPY daedalus/ ./daedalus/
COPY lauf.py schema.sql ./
COPY migrationen/ ./migrationen/

# Kein Wurzelbenutzer: der Dienst liest nur, er braucht keine Rechte am Abbild.
RUN adduser -D -H daedalus
USER daedalus

ENV PYTHONUNBUFFERED=1 TZ=Europe/Berlin
# Vorgabe ist der Sammeldienst; die Weboberflaeche setzt im Nomad-Job ihren
# eigenen Befehl. Kein ENTRYPOINT, damit beide gleich lesbar bleiben.
CMD ["python", "lauf.py", "--dienst"]
