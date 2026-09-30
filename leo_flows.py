"""Session-owned, backend-agnostic fixed-rate iperf3 flow service.

The service accepts logical names from a :class:`leo_lab.Lab`-like object and
translates them to its current backend's session-owned container identifiers.
It never accepts executable strings or invokes a shell: every backend call
receives a generated argv list.

Backends that opt in implement this small capability protocol::

    start_background(container_id: str, argv: list[str]) -> str
    stop_background(container_id: str, handle: str) -> None
    background_status(container_id: str, handle: str) -> str  # optional

The existing backend intentionally does not implement it yet.  In that case a
request returns the structured ``unavailable`` result rather than attempting a
foreground command or a shell workaround.
"""
from __future__ import annotations

import ipaddress
import math
import uuid
from typing import Any


class FlowService:
    """Create and supervise fixed-rate iperf3 flows within one Lab session."""

    _TERMINAL_STATUSES = {"completed", "stopped", "failed"}
    _BACKGROUND_METHODS = ("start_background", "stop_background")

    def __init__(self, lab: Any, backend: Any | None = None) -> None:
        self.lab = lab
        self._backend_override = backend
        self._flows: dict[str, dict[str, Any]] = {}

    def create_flow(
        self,
        *,
        source: str,
        destination: str,
        protocol: str,
        rate_mbps: int | float,
        duration_seconds: int,
        port: int = 5201,
    ) -> dict[str, Any]:
        """Start destination server and source client for a validated flow intent.

        ``status`` is ``running`` on success; invalid topology/session targets
        produce ``rejected`` and a missing background capability produces
        ``unavailable``. No command is executed by this service itself.
        """
        self._validate_intent(source, destination, protocol, rate_mbps, duration_seconds, port)
        source_record, destination_record = self._running_records(source, destination)
        if source_record is None:
            return self._rejected("source_not_running", source, destination)
        if destination_record is None:
            return self._rejected("destination_not_running", source, destination)
        if not self._has_path(source, destination):
            return self._rejected("no_topology_path", source, destination)

        backend = self._current_backend()
        if backend is None or not all(callable(getattr(backend, name, None))
                                      for name in self._BACKGROUND_METHODS):
            return {"status": "unavailable", "reason": "background_process_unsupported",
                    "source": source, "destination": destination}

        source_cid = source_record["container_id"]
        destination_cid = destination_record["container_id"]
        self._validate_backend_ownership(backend, source_cid, destination_cid)
        destination_ip = self._service_ip(destination_record)
        server_argv = ["iperf3", "-s", "-1", "-p", str(port)]
        client_argv = ["iperf3", "-c", destination_ip, "-p", str(port), "-t",
                       str(duration_seconds), "-b", f"{self._rate_token(rate_mbps)}M"]
        if protocol == "udp":
            client_argv.append("-u")

        flow_id = uuid.uuid4().hex
        flow = {
            "flow_id": flow_id,
            "source": source,
            "destination": destination,
            "protocol": protocol,
            "rate_mbps": rate_mbps,
            "duration_seconds": duration_seconds,
            "port": port,
            "status": "running",
            "server_argv": list(server_argv),
            "client_argv": list(client_argv),
            "server_handle": None,
            "client_handle": None,
        }
        try:
            flow["server_handle"] = backend.start_background(destination_cid, list(server_argv))
            flow["client_handle"] = backend.start_background(source_cid, list(client_argv))
        except Exception as exc:
            flow["status"] = "failed"
            self._flows[flow_id] = flow
            if flow["server_handle"] is not None:
                try:
                    backend.stop_background(destination_cid, flow["server_handle"])
                except Exception:
                    pass
            return {"status": "failed", "flow": self._public_flow(flow), "error": str(exc)}

        self._flows[flow_id] = flow
        return {"status": "running", "flow": self._public_flow(flow)}

    def preview_flow(self, *, source: str, destination: str, protocol: str,
                     rate_mbps: int | float, duration_seconds: int,
                     port: int = 5201) -> dict[str, Any]:
        """Validate a flow request and return the generated argv without starting it."""
        self._validate_intent(source, destination, protocol, rate_mbps, duration_seconds, port)
        source_record, destination_record = self._running_records(source, destination)
        if source_record is None:
            return self._rejected("source_not_running", source, destination)
        if destination_record is None:
            return self._rejected("destination_not_running", source, destination)
        if not self._has_path(source, destination):
            return self._rejected("no_topology_path", source, destination)
        destination_ip = self._service_ip(destination_record)
        client_argv = ["iperf3", "-c", destination_ip, "-p", str(port), "-t",
                       str(duration_seconds), "-b", f"{self._rate_token(rate_mbps)}M"]
        if protocol == "udp":
            client_argv.append("-u")
        return {"status": "ready", "source": source, "destination": destination,
                "protocol": protocol, "rate_mbps": rate_mbps,
                "duration_seconds": duration_seconds, "port": port,
                "server_argv": ["iperf3", "-s", "-1", "-p", str(port)],
                "client_argv": client_argv}

    def get_status(self, flow_id: str) -> dict[str, Any]:
        """Return one flow's status, refreshing it if the backend exposes status."""
        flow = self._flows.get(flow_id)
        if flow is None:
            raise ValueError("Flow not found")
        self._refresh(flow)
        return self._public_flow(flow)

    def status(self, flow_id: str | None = None) -> dict[str, Any]:
        """Return one status or the session-owned flow list."""
        if flow_id is not None:
            return self.get_status(flow_id)
        for flow in self._flows.values():
            self._refresh(flow)
        return {"flows": [self._public_flow(flow) for flow in self._flows.values()]}

    def stop_source_flows(self, source: str) -> dict[str, Any]:
        """Stop all running flows sourced by this logical session node."""
        if not isinstance(source, str) or source not in getattr(self.lab, "nodes", {}):
            raise ValueError("Source node is not a running session-owned node")
        stopped: list[str] = []
        for flow in self._flows.values():
            self._refresh(flow)
            if flow["source"] != source or flow["status"] in self._TERMINAL_STATUSES:
                continue
            backend = self._current_backend()
            if backend is None or not callable(getattr(backend, "stop_background", None)):
                return {"status": "unavailable", "reason": "background_process_unsupported",
                        "source": source, "stopped_flow_ids": stopped}
            try:
                source_cid = self.lab.nodes[source]["container_id"]
                destination_cid = self.lab.nodes[flow["destination"]]["container_id"]
                backend.stop_background(source_cid, flow["client_handle"])
                backend.stop_background(destination_cid, flow["server_handle"])
                flow["status"] = "stopped"
                stopped.append(flow["flow_id"])
            except Exception as exc:
                flow["status"] = "failed"
                flow["error"] = str(exc)
        return {"status": "ok", "source": source, "stopped_flow_ids": stopped}

    def _current_backend(self) -> Any | None:
        return self._backend_override if self._backend_override is not None else getattr(self.lab, "backend", None)

    def _running_records(self, source: str, destination: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        nodes = getattr(self.lab, "nodes", {})
        source_record = nodes.get(source) if isinstance(source, str) else None
        destination_record = nodes.get(destination) if isinstance(destination, str) else None
        return (source_record if self._is_running_record(source_record) else None,
                destination_record if self._is_running_record(destination_record) else None)

    @staticmethod
    def _is_running_record(record: Any) -> bool:
        return isinstance(record, dict) and isinstance(record.get("container_id"), str) and record["container_id"]

    def _has_path(self, source: str, destination: str) -> bool:
        adjacency: dict[str, set[str]] = {name: set() for name in getattr(self.lab, "nodes", {})}
        for link in getattr(self.lab, "active_links", []):
            if not isinstance(link, dict):
                continue
            left, right = link.get("source"), link.get("target")
            if left in adjacency and right in adjacency:
                adjacency[left].add(right)
                adjacency[right].add(left)
        pending, visited = [source], {source}
        while pending:
            node = pending.pop()
            if node == destination:
                return True
            for neighbor in adjacency[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        return False

    @staticmethod
    def _validate_backend_ownership(backend: Any, *container_ids: str) -> None:
        owned = getattr(backend, "owned", None)
        if owned is not None and any(container_id not in owned for container_id in container_ids):
            raise ValueError("Container is not owned by this session")

    @staticmethod
    def _service_ip(record: dict[str, Any]) -> str:
        try:
            value = record["spec"]["ip"]
            return str(ipaddress.IPv4Interface(value).ip)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Destination node has no valid service IP") from exc

    @staticmethod
    def _rate_token(rate_mbps: int | float) -> str:
        return format(rate_mbps, "g")

    @staticmethod
    def _validate_intent(source: Any, destination: Any, protocol: Any,
                         rate_mbps: Any, duration_seconds: Any, port: Any) -> None:
        if not isinstance(source, str) or not source or not isinstance(destination, str) or not destination:
            raise ValueError("source and destination must be non-empty logical node names")
        if source == destination:
            raise ValueError("source and destination must differ")
        if protocol not in {"tcp", "udp"}:
            raise ValueError("protocol must be tcp or udp")
        if (isinstance(rate_mbps, bool) or not isinstance(rate_mbps, (int, float))
                or not math.isfinite(rate_mbps) or not 0 < rate_mbps <= 100000):
            raise ValueError("rate_mbps must be finite and within 0..100000")
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or not 1 <= duration_seconds <= 3600:
            raise ValueError("duration_seconds must be an integer from 1 to 3600")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer from 1 to 65535")

    @staticmethod
    def _rejected(reason: str, source: str, destination: str) -> dict[str, str]:
        return {"status": "rejected", "reason": reason, "source": source, "destination": destination}

    def _refresh(self, flow: dict[str, Any]) -> None:
        if flow["status"] in self._TERMINAL_STATUSES:
            return
        backend = self._current_backend()
        inspect = getattr(backend, "background_status", None)
        if not callable(inspect):
            return
        try:
            source_cid = self.lab.nodes[flow["source"]]["container_id"]
            client_status = inspect(source_cid, flow["client_handle"])
            if client_status in self._TERMINAL_STATUSES:
                flow["status"] = client_status
        except Exception as exc:
            flow["status"] = "failed"
            flow["error"] = str(exc)

    @staticmethod
    def _public_flow(flow: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in flow.items() if key not in {"server_handle", "client_handle"}}
