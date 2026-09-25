# -*- coding: utf-8 -*-
"""Collectors for the Prometheus inventory and UniFi Wi-Fi clients.

Both collectors only read the Prometheus HTTP API. Each precheck reflects exactly
the state the collector delivers later. A plain ``count(...)`` would not be safe
for Wi-Fi: a client can change its access point or its address while the number
of clients stays the same.

The Wi-Fi collector expects the series of `unpoller`
(https://github.com/unpoller/unpoller) in Prometheus; the inventory collector uses
the `host` label of your scrape targets.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .collector_arp import is_random_mac, normalize_mac
from .model import Observation, Relation, Source, address_key

METRIC_WIFI = "unpoller_client_uptime_seconds"
QUERY_WIFI = f'{METRIC_WIFI}{{wired="false"}}'
QUERY_WIFI_PRECHECK = (
    "count by (mac, ip, name, vlan, essid, ap_name, oui) "
    f"({QUERY_WIFI})"
)
QUERY_INVENTORY_PRECHECK = 'count by (host, instance) (up{host!=""})'

_MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
Json = dict[str, Any]
Caller = Callable[[str, dict[str, str] | None], Json]


def _text(value: Any) -> str:
    """Prometheus labels are strings; anything else counts as not seen."""
    return value.strip() if isinstance(value, str) else ""


def _mac(raw: Any) -> str:
    text = _text(raw).replace(":", " ")
    mac = normalize_mac(text)
    return mac if _MAC.fullmatch(mac) else ""


def _result(response: Json) -> list[Json]:
    """Carefully take a Prometheus vector out of an API response."""
    if not isinstance(response, dict) or response.get("status") != "success":
        raise RuntimeError("Prometheus does not report a successful query")
    data = response.get("data")
    series = data.get("result") if isinstance(data, dict) else None
    if not isinstance(series, list):
        raise RuntimeError("Prometheus response contains no vector")
    return [s for s in series if isinstance(s, dict)]


_LEADING_IP = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?$")


def _inventory_pairs(entries) -> list[Observation]:
    """Name and inventory address per host from (host, instance) pairs.

    The address is the real gain: the DNS server may not know any PTR records,
    but the inventory knows that 172.16.1.6 is a particular host. `127.0.0.1` is
    the exporter's view of itself and not an address in the network; a host with
    several addresses gets none - otherwise the order of the targets would decide
    which one applies.
    """
    hosts: set[str] = set()
    addresses: dict[str, set[str]] = {}
    for host, instance in entries:
        if not host:
            continue
        hosts.add(host)
        m = _LEADING_IP.match(instance)
        if m and not m.group(1).startswith("127."):
            addresses.setdefault(host, set()).add(m.group(1))
    out = [Observation(Relation.ATTRIBUTE, host, "name", host) for host in sorted(hosts)]
    for host in sorted(addresses):
        if len(addresses[host]) == 1:
            out.append(Observation(Relation.ATTRIBUTE, host, "inventory_address",
                                   next(iter(addresses[host]))))
    return out


def parse_inventory(response: Json) -> list[Observation]:
    """From the active targets: per unique host a name and, where unambiguous,
    its address."""
    if not isinstance(response, dict) or response.get("status") != "success":
        raise RuntimeError("Prometheus does not report a successful target query")
    data = response.get("data")
    targets = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(targets, list):
        raise RuntimeError("Prometheus response contains no active targets")

    pairs = []
    for target in targets:
        labels = target.get("labels") if isinstance(target, dict) else None
        if isinstance(labels, dict):
            pairs.append((_text(labels.get("host")), _text(labels.get("instance"))))
    return _inventory_pairs(pairs)


def _signature(observations: list[Observation]) -> tuple:
    return tuple((o.obj, o.key, o.value) for o in observations)


def _wifi_labels(response: Json) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    for series in _result(response):
        metric = series.get("metric")
        if isinstance(metric, dict):
            labels.append(metric)
    return labels


def wifi_signature(response: Json) -> tuple[tuple[str, ...], ...]:
    """The complete Wi-Fi state, without measured value and timestamp."""
    fields = ("ip", "name", "vlan", "essid", "ap_name", "oui")
    rows = []
    for labels in _wifi_labels(response):
        mac = _mac(labels.get("mac"))
        if mac:
            rows.append((mac, *(_text(labels.get(f)) for f in fields)))
    return tuple(sorted(set(rows)))


def parse_wifi(response: Json) -> list[Observation]:
    """UniFi labels become address, attachment and attributes.

    Every available label is used on its own, so a missing ``oui`` does not also
    cost the address and attachment of the same client. Random MACs get an explicit
    attribute: only then can a later view filter them out of "new device" without
    losing their technically useful assignments. More importantly, their
    observations are flagged as *volatile*, so reconciliation does not treat them
    as news in the first place.
    """
    out: dict[tuple[Relation, str, str], Observation] = {}
    for labels in _wifi_labels(response):
        mac = _mac(labels.get("mac"))
        if not mac:
            continue

        # If the device randomises its MAC, every observation of it is flagged as
        # volatile: tracked yes, reported no. Reconciliation then suppresses first
        # sighting and disappearance but keeps moves and address changes visible.
        volatile = is_random_mac(mac)

        def note(relation: Relation, key: str, value: str) -> None:
            if value:
                o = Observation(relation, mac, key, value, volatile=volatile)
                out[(relation, mac, key)] = o

        # `0.0.0.0` is reported by a client that has joined but has not received an
        # address via DHCP yet. That is not an address but an intermediate state -
        # it once showed up as "address_added 0.0.0.0 -> 172.16.11.232". Such
        # intermediate steps do not belong in the change report.
        ip = _text(labels.get("ip"))
        if ip and ip not in ("0.0.0.0", "::"):
            note(Relation.ADDRESS, address_key(ip), ip)
        ap = _text(labels.get("ap_name"))
        note(Relation.ATTACHMENT, "attachment", f"ap:{ap}" if ap else "")
        for field in ("name", "vlan", "essid", "oui"):
            note(Relation.ATTRIBUTE, field, _text(labels.get(field)))
        if volatile:
            # In addition to the flag on the observation also as an attribute, so
            # the UI can filter by it.
            note(Relation.ATTRIBUTE, "random_mac", "yes")

    return [out[k] for k in sorted(out, key=lambda x: (x[1], x[0].value, x[2]))]


class _Prometheus:
    def __init__(self, base_url: str, timeout: int, caller: Caller | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._caller = caller or self._http_json

    def _http_json(self, path: str, params: dict[str, str] | None = None) -> Json:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            if response.status != 200:
                raise RuntimeError(f"Prometheus answers with HTTP {response.status}")
            try:
                data = json.load(response)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise RuntimeError("Prometheus returned no valid JSON") from e
        if not isinstance(data, dict):
            raise RuntimeError("Prometheus returned no JSON object")
        return data

    def _query(self, expression: str) -> Json:
        return self._caller("/api/v1/query", {"query": expression})


class PrometheusInventory(_Prometheus):
    """Managed hosts from ``/api/v1/targets``."""

    def __init__(self, base_url: str, name: str = "inventory", timeout: int = 8,
                 caller: Caller | None = None) -> None:
        super().__init__(base_url, timeout, caller)
        self._last_signature: tuple | None = None
        self.source = Source(
            name=name,
            # Targets say nothing about Wi-Fi attachments or IP assignments.
            responsible_for=frozenset({Relation.ATTRIBUTE}),
            missing_threshold=2,
        )

    def precheck_unchanged(self) -> bool:
        try:
            pairs = [(_text(s["metric"].get("host")), _text(s["metric"].get("instance")))
                     for s in _result(self._query(QUERY_INVENTORY_PRECHECK))
                     if isinstance(s.get("metric"), dict)]
            signature = _signature(_inventory_pairs(pairs))
            return bool(signature and self._last_signature is not None
                        and signature == self._last_signature)
        except Exception:  # noqa: BLE001
            # A broken cheap query must never prevent a full query.
            return False

    def collect(self) -> list[Observation]:
        observations = parse_inventory(self._caller("/api/v1/targets", None))
        if not observations:
            raise RuntimeError("Prometheus returned no targets with host names")
        self._last_signature = _signature(observations)
        return observations


class PrometheusWifi(_Prometheus):
    """Wi-Fi clients from the series delivered by unpoller."""

    def __init__(self, base_url: str, name: str = "wifi", timeout: int = 8,
                 caller: Caller | None = None) -> None:
        super().__init__(base_url, timeout, caller)
        self._last_signature: tuple[tuple[str, ...], ...] | None = None
        self.source = Source(
            name=name,
            responsible_for=frozenset({
                Relation.ADDRESS, Relation.ATTACHMENT, Relation.ATTRIBUTE,
            }),
            missing_threshold=2,
        )

    def precheck_unchanged(self) -> bool:
        try:
            signature = wifi_signature(self._query(QUERY_WIFI_PRECHECK))
            return bool(signature and self._last_signature is not None
                        and signature == self._last_signature)
        except Exception:  # noqa: BLE001
            return False

    def collect(self) -> list[Observation]:
        response = self._query(QUERY_WIFI)
        signature = wifi_signature(response)
        observations = parse_wifi(response)
        if not signature or not observations:
            # A network with Wi-Fi has clients. An empty series is therefore an
            # outage, not the statement "all gone".
            raise RuntimeError("Prometheus returned no usable Wi-Fi clients")
        self._last_signature = signature
        return observations
