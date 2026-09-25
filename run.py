# -*- coding: utf-8 -*-
"""Run a collection.

    python run.py              # all sources, once
    python run.py --service    # permanently, every source at its own pace
    python run.py --dry        # only ask, write nothing
    python run.py --only arp   # one source

The site (firewall, switches, networks, Kea, Prometheus) comes from
`daedalus.toml` (see `daedalus.example.toml`, path via DAEDALUS_CONFIG).
Credentials come from the environment, never from the source code or the config:

    DAEDALUS_DSN            postgresql://daedalus:...@db:5432/daedalus
    DAEDALUS_SNMP_FIREWALL  SNMP community of the firewall (ARP table)
    DAEDALUS_SNMP_SWITCH    SNMP community of the switches
    DAEDALUS_SNMP_VIA       optional: run SNMP via this host over SSH (development)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from daedalus import config as site_config
from daedalus import db as database
from daedalus import migration
from daedalus.chain import neighborhood
from daedalus.collector import run_round
from daedalus.collector_arp import OID_ARP, ArpCollector
from daedalus.collector_kea import KeaConfig, KeaLeases
from daedalus.collector_portstate import SwitchPortState
from daedalus.collector_prometheus import PrometheusInventory, PrometheusWifi
from daedalus.collector_switch import SwitchMacs, SwitchNeighbors
from daedalus.service import Job, Service, summary
from daedalus.store import MemoryStore, PgStore
from daedalus.timeutil import display, now


def snmp_via_host(host: str):
    """Run SNMP through another host - **for development only**.

    Useful when your workstation cannot reach the switches or has no net-snmp. In
    production the container image brings its own tools. Slow (every call starts a
    throwaway container on that host), but it changes nothing there.
    """
    def call(target: str, community: str, oid: str) -> str:
        command = (f"docker run --rm --net=host alpine:3.20 sh -c "
                   f"'apk add --no-cache net-snmp-tools >/dev/null 2>&1; "
                   f"snmpbulkwalk -v2c -c \"{community}\" -Cr40 -OQn -t 6 -r 1 "
                   f"{target} {oid}'")
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes",
                            f"root@{host}", command],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"{target}: {(r.stderr or 'no output').strip()[:160]}")
        return r.stdout
    return call


def build_collectors(cfg: site_config.Config, only: str = "") -> list:
    firewall_community = os.environ.get("DAEDALUS_SNMP_FIREWALL", "")
    switch_community = os.environ.get("DAEDALUS_SNMP_SWITCH", "")
    via = os.environ.get("DAEDALUS_SNMP_VIA", "")
    snmp = snmp_via_host(via) if via else None

    collectors = []

    if cfg.firewall and firewall_community:
        arp = ArpCollector(cfg.firewall, firewall_community)
        if snmp:
            arp._call = lambda: snmp(cfg.firewall, firewall_community, OID_ARP)
        collectors.append(arp)

    if cfg.prometheus_url:
        if cfg.prometheus_inventory:
            collectors.append(PrometheusInventory(cfg.prometheus_url))
        if cfg.prometheus_wifi:
            collectors.append(PrometheusWifi(cfg.prometheus_url))
    if cfg.kea_nodes:
        collectors.append(KeaLeases(cfg.kea_nodes))
    if cfg.kea_config_url:
        collectors.append(KeaConfig(cfg.kea_config_url))

    if switch_community:
        for sw in cfg.switches:
            nb = SwitchNeighbors(sw.name, sw.address, switch_community)
            # The MAC table needs the uplink list - otherwise half the network hangs
            # off an uplink. It reads the list from the neighbour collector, which is
            # why LLDP always comes BEFORE its MAC table in the list.
            mc = SwitchMacs(sw.name, sw.address, switch_community, neighbors=nb)
            if snmp:
                nb._call = lambda oid, a=sw.address: snmp(a, switch_community, oid)
                mc._call = lambda oid, a=sw.address: snmp(a, switch_community, oid)
            # Port state after the neighbourhood: it asks which ports are uplinks,
            # so only their link changes are reported.
            ps = SwitchPortState(sw.name, sw.address, switch_community, neighbors=nb)
            collectors.extend((nb, mc, ps))

    if only:
        collectors = [c for c in collectors if only in c.source.name]
    return collectors


def register_sources(db, collectors, cfg) -> None:
    """The sources must exist in the database - `run` references them by foreign
    key. That is intentional: an observation without a known origin must not exist."""
    with db.cursor() as c:
        for col in collectors:
            s = col.source
            c.execute("""INSERT INTO source (name, responsible_for, missing_threshold,
                                             interval_seconds)
                         VALUES (%s,%s,%s,%s)
                         ON CONFLICT (name) DO UPDATE
                            SET responsible_for   = EXCLUDED.responsible_for,
                                missing_threshold = EXCLUDED.missing_threshold,
                                interval_seconds  = EXCLUDED.interval_seconds""",
                      (s.name, [r.value for r in s.responsible_for],
                       s.missing_threshold, cfg.interval_for(s.name)))


def as_service(store, collectors, db, cfg) -> int:
    """Run permanently. Every source at its own pace, one after the other."""
    jobs = [Job(c, interval=cfg.interval_for(c.source.name)) for c in collectors]
    print(f"Service started, {len(jobs)} sources")
    for j in sorted(jobs, key=lambda x: (x.interval, x.name)):
        print(f"  {j.name:<16} every {int(j.interval // 60)} minutes")
    print("\nFrom now on only changes are reported.\n", flush=True)

    def report(r, job):
        line = summary(r, job)
        if line:
            print(line, flush=True)

    try:
        Service(jobs).run(store, report)
    except KeyboardInterrupt:
        print("\nService stopped.")
    finally:
        if db:
            db.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Run a collection")
    p.add_argument("--service", action="store_true",
                   help="run permanently, every source at its own pace")
    p.add_argument("--dry", action="store_true",
                   help="only ask, write nothing (in memory)")
    p.add_argument("--only", default="", help="only sources whose name contains this")
    p.add_argument("--config", default=None, help="path to daedalus.toml")
    args = p.parse_args()

    try:
        cfg = site_config.load(args.config)
    except site_config.ConfigError as e:
        print(f"Configuration error: {e}")
        return 2
    site_config.apply(cfg)

    collectors = build_collectors(cfg, args.only)
    if not collectors:
        print("No collectors - are the SNMP communities missing in the environment, "
              "or is the configuration empty?")
        return 2

    dsn = os.environ.get("DAEDALUS_DSN", "")
    if args.dry or not dsn:
        store, db = MemoryStore(), None
        print("DRY RUN - nothing is written\n")
    else:
        db = database.connect(dsn)
        if database.ensure_schema(db):
            print("Database schema created.", flush=True)
        new = migration.apply(db)
        if new:
            print(f"Migrations applied: {', '.join(new)}", flush=True)
        register_sources(db, collectors, cfg)
        store = PgStore(db)

    # Counter readings bypass reconciliation and go straight into their table.
    # Wired only here, because only now is the store known (database or memory).
    for c in collectors:
        if isinstance(c, SwitchPortState):
            c.counter_sink = lambda readings, s=store: s.remember_counters(now(), readings)

    if args.service:
        return as_service(store, collectors, db, cfg)

    timestamp = now()
    print(f"Collection run {display(timestamp)}  -  {len(collectors)} sources\n")

    # LLDP is directly before its MAC table in the list and therefore runs first;
    # without that the MAC table knows no uplinks and rather reports nothing.
    results = run_round(store, collectors, timestamp)

    width = max(len(r.source) for r in results)
    for r in results:
        print(f"  {r.source:<{width}}  {str(r).split(': ', 1)[1]}")

    changes = [c for r in results for c in r.changes]
    print(f"\n{sum(r.observations for r in results)} observations, "
          f"{len(changes)} changes, "
          f"{sum(1 for r in results if not r.successful)} failures")

    for c in changes[:15]:
        print(f"  {c.kind.value:<18} {c.obj}  {c.before or ''} -> {c.after or ''}")
    if len(changes) > 15:
        print(f"  ... and {len(changes) - 15} more")

    # What do we know about the cabling now?
    graph = neighborhood(store)
    if graph:
        print("\nNeighbourhood:")
        for sw in sorted(graph):
            neighbors = sorted({n for n, _, _ in graph[sw]})
            print(f"  {sw:<6} -> {', '.join(neighbors)}")

    if db:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
