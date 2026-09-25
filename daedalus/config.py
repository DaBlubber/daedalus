# -*- coding: utf-8 -*-
"""Site configuration: `daedalus.toml`.

Everything that describes *your* network lives here - firewall, switches, networks,
Kea, Prometheus, time zone. Secrets do not: the database DSN and the SNMP
communities only come from the environment (see `.env.example`).

The file is found via `DAEDALUS_CONFIG` (default: `./daedalus.toml`). Start from
`daedalus.example.toml`.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = "daedalus.toml"

# Interval per source in seconds. The sources age at different speeds - a shared
# interval would be the most expensive option. Keys are source names or prefixes.
DEFAULT_INTERVALS = {
    "firewall-arp": 300,    # ARP ages out in minutes
    "inventory":    900,    # the host list rarely changes
    "kea":          900,    # leases live for hours, names even longer
    "dhcp-config":  900,    # conditional GET, costs zero bytes when unchanged
    "wifi":         300,    # clients come and go
    "fdb-":         300,    # MAC table, ages out after ~5 minutes
    "lldp-":       1800,    # the neighbourhood only changes when re-plugging
    "port-":        300,    # link, VLAN, PoE, error counters
}


@dataclass
class Switch:
    name: str
    address: str
    model: str = ""
    ports: int = 28


@dataclass
class Network:
    cidr: str
    vlan: int | None
    name: str


@dataclass
class Config:
    timezone: str = "UTC"
    root_switch: str = ""
    firewall: str = ""                       # SNMP target for the ARP table
    firewall_name: str = "GATEWAY"           # label on the map
    firewall_model: str = ""
    firewall_addresses: list[str] = field(default_factory=list)
    prometheus_url: str = ""
    prometheus_inventory: bool = True
    prometheus_wifi: bool = True
    kea_nodes: tuple[str, ...] = ()
    kea_config_url: str = ""
    local_domain: str = ""
    copper_ports: int = 24
    switches: list[Switch] = field(default_factory=list)
    networks: list[Network] = field(default_factory=list)
    own_networks: list[str] = field(default_factory=list)
    ping_only_networks: list[str] = field(default_factory=list)
    intervals: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_INTERVALS))

    def interval_for(self, source: str) -> int:
        for key, seconds in self.intervals.items():
            if source == key or source.startswith(key):
                return seconds
        return 600


class ConfigError(RuntimeError):
    pass


def load(path: str | os.PathLike | None = None) -> Config:
    """Read and validate the configuration file."""
    p = Path(path or os.environ.get("DAEDALUS_CONFIG", DEFAULT_PATH))
    if not p.is_file():
        raise ConfigError(f"configuration not found: {p} "
                          "(copy daedalus.example.toml and set DAEDALUS_CONFIG)")
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    try:
        return _parse(raw)
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"{p}: {e}") from e


def _parse(raw: dict) -> Config:
    fw = raw.get("firewall", {})
    prom = raw.get("prometheus", {})
    dhcp = raw.get("dhcp", {})
    actions = raw.get("actions", {})
    switches = [Switch(name=name, address=s["address"], model=s.get("model", ""),
                       ports=int(s.get("ports", 28)))
                for name, s in (raw.get("switches") or {}).items()]
    networks = [Network(cidr=n["cidr"], vlan=n.get("vlan"), name=n.get("name", n["cidr"]))
                for n in raw.get("networks") or []]
    intervals = dict(DEFAULT_INTERVALS)
    intervals.update({k: int(v) for k, v in (raw.get("intervals") or {}).items()})
    cfg = Config(
        timezone=raw.get("timezone", "UTC"),
        root_switch=raw.get("root_switch", switches[0].name if switches else ""),
        firewall=fw.get("address", ""),
        firewall_name=fw.get("name", "GATEWAY"),
        firewall_model=fw.get("model", ""),
        firewall_addresses=list(fw.get("addresses")
                                or ([fw["address"]] if fw.get("address") else [])),
        prometheus_url=prom.get("url", ""),
        prometheus_inventory=bool(prom.get("inventory", True)),
        prometheus_wifi=bool(prom.get("wifi", True)),
        kea_nodes=tuple(dhcp.get("kea_nodes", ())),
        kea_config_url=dhcp.get("config_url", ""),
        local_domain=dhcp.get("local_domain", ""),
        copper_ports=int(raw.get("copper_ports", 24)),
        switches=switches,
        networks=networks,
        own_networks=list(actions.get("own_networks") or [n.cidr for n in networks]),
        ping_only_networks=list(actions.get("ping_only_networks") or []),
        intervals=intervals,
    )
    if cfg.root_switch and cfg.switches and cfg.root_switch not in {s.name for s in switches}:
        raise ValueError(f"root_switch '{cfg.root_switch}' is not one of the switches")
    return cfg


def apply(cfg: Config) -> None:
    """Hand the site description to the modules that need it."""
    from . import actions, collector_kea, state, timeutil
    timeutil.set_local_timezone(cfg.timezone)
    state.configure(
        networks=[(n.cidr, n.vlan, n.name) for n in cfg.networks],
        switches={s.name: (s.address, s.model, s.ports) for s in cfg.switches},
        copper_ports=cfg.copper_ports,
        site={
            "gateway": {"label": cfg.firewall_name, "model": cfg.firewall_model,
                        "addresses": cfg.firewall_addresses},
            "dhcp": {"label": "KEA DHCP", "nodes": list(cfg.kea_nodes)},
        },
    )
    if cfg.own_networks:
        actions.configure(cfg.own_networks, cfg.ping_only_networks)
    collector_kea.LOCAL_DOMAIN = cfg.local_domain
