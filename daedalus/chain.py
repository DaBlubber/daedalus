# -*- coding: utf-8 -*-
"""The chain: "from where to where".

This is the core function of the whole tool - the question nobody can answer
today:

    ROUTER -> BB8 port 3+4 -> C3PO port 12 -> laptop

It is built from three sources that are useless on their own and everything
together:

| Source | says | useless alone because |
|---|---|---|
| ARP (firewall) | IP <-> MAC | it knows no port |
| MAC table (switch) | MAC <-> port | it knows no direction |
| LLDP (switch) | port <-> neighbour port | it knows no end devices |

## The direction

LLDP delivers an **undirected** graph: c3po says "bb8 is on gi25", bb8 says
"c3po is on gi1". Neither of them says where *up* is.

Up is therefore **defined**: the root is the switch at the firewall. From there
every switch gets its distance (breadth-first search), and the chain of a device
always runs from larger to smaller distance.

**A real network is not a tree.** A switch can hang off two others at once. The
breadth-first search picks the shortest path; if there are several equally short
ones, the chain says so (`unambiguous=False`) instead of silently picking one. A
map that guesses silently is worse than one that admits it does not know.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .model import Relation


@dataclass(frozen=True)
class Link:
    """One step in the chain."""
    kind: str         # 'switch' | 'device'
    name: str         # 'c3po' or the MAC
    port: str = ""    # the port through which it continues
    measured: bool = True   # False = inferred, not observed

    def __str__(self) -> str:
        return f"{self.name} {self.port}".strip()


@dataclass
class Chain:
    links: tuple[Link, ...]
    unambiguous: bool = True
    complete: bool = True         # False = does not reach the root

    def __str__(self) -> str:
        return " -> ".join(str(link) for link in self.links)

    def __bool__(self) -> bool:
        return bool(self.links)


# Two key forms share the same colon:
#   `c3po:gi12`  switch and port
#   `ap:Luke`    an access point
# Splitting blindly at the first colon turns `ap:Luke` into the node "ap" with the
# port "Luke" - and the chain of a Wi-Fi client ends in nothing. That happened
# exactly like this on the first real run.
def _node(attachment: str) -> str:
    """The device something is attached to: `c3po:gi12` -> `c3po`, `ap:Luke` -> `Luke`"""
    if attachment.startswith("ap:"):
        return attachment[3:]
    return attachment.split(":", 1)[0]


def _port(attachment: str) -> str:
    """The port it is attached to. An access point has none."""
    if attachment.startswith("ap:"):
        return ""
    return attachment.split(":", 1)[1] if ":" in attachment else ""


_switch = _node          # old name, same meaning


def neighborhood(store) -> dict[str, list[tuple[str, str, str]]]:
    """The switch graph from the open LLDP observations.

    `{switch: [(neighbour, own port, neighbour port), ...]}` - in **both**
    directions. LLDP is often reported by one side only: a switch whose SNMP is
    silent right now would otherwise be cut off from the network although its
    neighbour sees it.
    """
    out: dict[str, list[tuple[str, str, str]]] = {}
    connections = store.all_open(Relation.CONNECTION)

    # A switch reports itself under the name we give it (`l337`), its neighbours
    # report it via LLDP under its system name (`L337`). Without aligning them they
    # become two nodes and l337 hangs in the network twice. Only names known as own
    # switches are aligned; access points keep their spelling (`ap:Luke`).
    own = {_switch(i.obj).lower(): _switch(i.obj) for i in connections}

    def align(name: str) -> str:
        return own.get(name.lower(), name)

    for i in connections:
        here, there = align(_switch(i.obj)), align(_switch(i.value))
        p_here, p_there = _port(i.obj), _port(i.value)
        if not here or not there or here == there:
            continue
        out.setdefault(here, []).append((there, p_here, p_there))
        reverse = (here, p_there, p_here)
        if reverse not in out.setdefault(there, []):
            out[there].append(reverse)
    return out


def distances(graph: dict, root: str) -> tuple[dict[str, int], dict[str, list]]:
    """Breadth-first search from the root. Returns distance and predecessors per switch.

    The predecessor is a **list**: if there are two equally short paths, both are
    in it. That is exactly how the chain recognises that it is not unambiguous.
    """
    distance = {root: 0}
    previous: dict[str, list] = {root: []}
    queue = deque([root])
    while queue:
        here = queue.popleft()
        for neighbor, p_here, p_there in graph.get(here, []):
            new = distance[here] + 1
            if neighbor not in distance:
                distance[neighbor] = new
                previous[neighbor] = [(here, p_there, p_here)]
                queue.append(neighbor)
            elif distance[neighbor] == new:
                entry = (here, p_there, p_here)
                if entry not in previous[neighbor]:
                    previous[neighbor].append(entry)
    return distance, previous


def chain(store, obj: str, root: str) -> Chain:
    """The path from the root to a device.

    `obj` is a MAC (or a switch name, then the chain ends there).
    """
    graph = neighborhood(store)
    distance, previous = distances(graph, root)

    # Where is the device attached? A switch name needs no attachment.
    if obj in graph or obj == root:
        start_switch, last_port, device = obj, "", None
    else:
        attachment = store.open_for(Relation.ATTACHMENT, obj, "attachment")
        if attachment is None:
            return Chain((), complete=False)
        start_switch = _switch(attachment.value)
        last_port = _port(attachment.value)
        device = obj

    if start_switch not in distance:
        # The switch is in no known neighbourhood. That is an honest partial answer:
        # "attached to c3po port 12, path there unknown".
        links = [Link("switch", start_switch, last_port)]
        if device:
            links.append(Link("device", device))
        return Chain(tuple(links), complete=False)

    # Build it up from the root
    backwards: list[Link] = []
    unambiguous = True
    here, port_down = start_switch, last_port
    while True:
        backwards.append(Link("switch", here, port_down))
        parents = previous.get(here) or []
        if not parents:
            break
        if len(parents) > 1:
            unambiguous = False
        # The entry is (parent, port on the CHILD, port on the PARENT). The parent
        # shows its own port - the one through which it reaches the child. The port
        # on the child would be the view from below and does not belong here; for
        # an access point it is even a MAC.
        parent_name, _port_on_child, port_on_parent = parents[0]
        here, port_down = parent_name, port_on_parent

    links = list(reversed(backwards))
    if device:
        links.append(Link("device", device))
    return Chain(tuple(links), unambiguous=unambiguous)


def who_was_here(store, attachment: str) -> list[tuple[str, object, object]]:
    """"Who is attached to this port - and who was attached before?"

    After "where is the device?" the second most frequent question, and with the
    interval model it comes for free: all assignments whose *value* is this port,
    newest first.
    """
    hits = []
    for i in store.all_with_value(Relation.ATTACHMENT, attachment):
        hits.append((i.obj, i.since, i.until))
    return sorted(hits, key=lambda x: x[1], reverse=True)
