# Daedalus

*[Deutsche Version](README.de.md)*

A small **network inventory with history** for a home lab or small office: which
device is plugged into which switch port, which IP it had when, and what changed.
Written in Python, backed by PostgreSQL, with a single-page web UI. Code, comments
and UI are in German.

```text
bb8 port 1 → c3po port 12 → b4:2e:99:1c:0d:7a
```

That chain is built from three sources that are useless on their own:

| Source | tells you | useless alone because |
|---|---|---|
| ARP (firewall, SNMP) | IP ↔ MAC | knows no port |
| MAC table (switch, SNMP) | MAC ↔ port | knows no direction |
| LLDP (switch, SNMP) | port ↔ neighbour port | knows no end devices |

plus Kea DHCP leases/reservations (names), UniFi Wi-Fi clients and the Ansible
inventory via Prometheus.

## Design in four rules

1. **Intervals, not snapshots.** A state that stays the same keeps its interval open;
   storage grows with *changes*, not with *time*.
2. **Absence ends an interval only if the responsible source succeeded.** A switch
   that did not answer must not make 50 devices "disappear".
3. **Only the responsible source may judge.** The DHCP collector cannot end a port
   connection just because it does not see ports.
4. **Imports are idempotent**, and the first run of any (new) source marks everything
   as existing inventory, not as "new".

Other details worth a look: uplink vs. access port detection (LLDP neighbour *or* port
channel), randomised phone MACs tracked but not reported, cheap pre-checks
(`ifLastChange`, grouped counts) before expensive walks, and a service that stays
quiet when nothing changed.

## Layout

| Path | What |
|---|---|
| `schema.sql`, `migrationen/` | PostgreSQL schema and migrations (applied on start) |
| `daedalus/abgleich.py` | the reconciliation rules – the only place that decides new/moved/gone |
| `daedalus/sammler_*.py` | collectors: ARP, switch MAC table + LLDP, port state, Kea, Prometheus |
| `daedalus/kette.py` | "from where to where" – the port chain |
| `daedalus/dienst.py` | scheduler: every source at its own pace, staggered, with back-off |
| `daedalus/web.py`, `daedalus/web/` | FastAPI + single-page UI |
| `lauf.py` | run once, as a service, or dry |
| `tests/` | every rule test runs twice: in memory **and** against real PostgreSQL |

## Running

```bash
pip install "psycopg[binary]" fastapi uvicorn pytest
python -m pytest                 # database tests are skipped without DAEDALUS_DSN
python lauf.py --trocken         # query all sources, write nothing
python lauf.py --dienst          # run as a service
```

Configuration comes from the environment only (see `.env.beispiel` and the docstring
of `lauf.py`): `DAEDALUS_DSN`, SNMP communities for firewall and switches, and the
Kea/Prometheus endpoints. The `Dockerfile` builds one image for collector and web UI
(net-snmp, nmap for the optional scan action, runs without root).

The collectors are written against the author's hardware (Sophos firewall, Cisco SMB
switches, Kea, UniFi via unpoller). Other vendors mostly need the OIDs in
`sammler_switch.py` / `sammler_portzustand.py` checked.

## Test data

The captures under `tests/proben/` are real SNMP/Kea/UniFi responses that were
**pseudonymised** before publication: addresses moved to `172.16.0.0/16`, MAC
addresses and device names replaced by stable pseudonyms. Structure and edge cases are
kept, so the tests still exercise the real formats (e.g. four different MAC notations
from different net-snmp builds).

## License

[MIT](LICENSE)
