"""Real isolated nodes; no switches, host links, management NAT or global cleanup."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: bytes
    timed_out: bool
    truncated: bool


class ContainernetBackend:
    def __init__(self) -> None:
        if os.geteuid() != 0:
            raise RuntimeError('Containernet apply requires root; drafts and queries do not grant privileges')
        try:
            import docker
            from mininet.net import Containernet
            from mininet.node import Docker
        except ModuleNotFoundError as exc:
            interpreter = sys.executable
            raise RuntimeError(
                f"Backend dependency '{exc.name}' is unavailable in {interpreter}. "
                "Launch with ./run-desktop.sh so root uses the project virtualenv."
            ) from exc

        class LeoDocker(Docker):
            """Containernet Docker host without Bash bracketed-paste escape output."""

            def startShell(self, *args: Any, **kwargs: Any) -> None:
                super().startShell(*args, **kwargs)
                if self.shell:
                    self.cmd("bind 'set enable-bracketed-paste off'")

        self.client: Any = docker.from_env(timeout=15)
        try:
            self.net = Containernet(controller=None, build=False)
        except BaseException:
            self.client.close()
            raise
        self.owned: dict[str, Any] = {}
        self._docker_cls: Any = LeoDocker
        self.terminals: dict[str, list[Any]] = {}
        self.links: list[dict[str, str]] = []
        self.subnet_counter = 0

    def create(self, node: dict[str, str]) -> str:
        # Resolve only a local immutable image: never implicitly pull packages/images.
        image = self.client.images.get(node['image'])
        # Keep "<name>-ethN" within Linux IFNAMSIZ (15 visible characters).
        name = 'leo' + uuid.uuid4().hex[:6]
        host: Any = None
        role = node['role']

        try:
            host = self.net.addDocker(name, dimage=node['image'], dcmd='/bin/bash',
                                      network_mode='none', publish_all_ports=False,
                                      volumes=[], ip=None, cls=self._docker_cls)
            cid = host.did
            self.owned[cid] = {'host': host, 'service_ip': node['ip'].split('/')[0],
                               'name': node['name'], 'role': node['role']}
            container = self.client.containers.get(cid)
            if container.status != 'running' or container.attrs['Image'] != image.id:
                raise RuntimeError('Container is not running the requested local image')
            
            # Network setup
            commands = [
                ['ip', 'link', 'set', 'lo', 'up'],
                ['ip', 'addr', 'add', self.owned[cid]['service_ip'] + '/32', 'dev', 'lo']
            ]
            for cmd in commands:
                self._exec(container, cmd)
                
            forwarding = '1' if role in {'satellite', 'gateway'} else '0'
            self._exec(container, ['sysctl', '-w', f'net.ipv4.ip_forward={forwarding}'])
            return str(cid)

        except BaseException:
            creation = getattr(host, 'dc', None)
            cid = creation.get('Id') if isinstance(creation, dict) else None
            if cid:
                # Since we didn't finish create, we don't have the dict yet, but let's try to cleanup.
                # We can't use self.owned[cid] here if it wasn't set.
                # Let's use a temporary host for cleanup.
                self._remove(cid, host)
            raise

    def query(self, cid: str) -> dict[str, Any]:
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        container = self.client.containers.get(cid)
        data: dict[str, Any] = {'container_id': cid, 'state': container.status, 'ipv4': [],
                                'network_scope': 'isolated_loopback',
                                'notice': '隔離節點：服務 IP 在 lo，尚無衛星鏈路或節點間連通。'}
        if container.status != 'running':
            return data
        result = self._exec(container, ['ip', '-j', '-4', 'addr', 'show'])
        interfaces = json.loads(result.output)
        data['interfaces'] = interfaces
        raw_result = self._exec(container, ['ip', 'addr', 'show'])
        raw_text = (raw_result.output.decode(errors='replace')
                    if isinstance(raw_result.output, bytes) else str(raw_result.output))
        data['interface_details'] = self._split_interface_details(raw_text)
        data['ipv4'] = [f"{addr['local']}/{addr['prefixlen']}"
                        for interface in interfaces for addr in interface.get('addr_info', [])
                        if addr.get('family') == 'inet']
        return data

    @staticmethod
    def _split_interface_details(raw_text: str) -> dict[str, str]:
        """Preserve exact ``ip addr`` sections for per-interface inspection."""
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in raw_text.splitlines():
            match = re.match(r'^\d+:\s+([^:@]+)', line)
            if match:
                current = str(match.group(1))
                sections[current] = [line]
            elif current:
                sections[current].append(line)
        return {name: '\n'.join(lines) for name, lines in sections.items()}

    def delete(self, cid: str) -> None:
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        record = self.owned[cid]
        self._remove(cid, record['host'])

    @staticmethod
    def _exec(container: Any, command: list[str]) -> Any:
        result = container.exec_run(command)
        if result.exit_code:
            output = result.output.decode(errors='replace') if isinstance(result.output, bytes) else str(result.output)
            raise RuntimeError(output or f"Command failed ({result.exit_code}): {' '.join(command)}")
        return result

    def open_terminal(self, cid: str) -> dict[str, Any]:
        """Open Mininet's persistent xterm for an owned Docker host."""
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        if not os.environ.get('DISPLAY'):
            raise RuntimeError('xterm requires an X11 DISPLAY (XWayland is supported)')
        if not shutil.which('xterm') or not shutil.which('docker'):
            raise RuntimeError('xterm and the Docker CLI must be installed')
        from mininet.term import makeTerm
        record = self.owned[cid]
        processes = makeTerm(record['host'], title=record['name'], term='xterm') or []
        if not processes:
            raise RuntimeError('Mininet could not open xterm; check that the node is running')
        self.terminals.setdefault(cid, []).extend(processes)
        for process in processes:
            try:
                code = process.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                continue
            raise RuntimeError(f'xterm exited immediately ({code}); check DISPLAY/Xauthority')
        return {'pids': [process.pid for process in processes], 'node': record['name']}

    def _close_terminals(self, cid: str) -> None:
        for process in self.terminals.get(cid, []):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self.terminals.pop(cid, None)

    def interactive_command(self, cid: str, cmd: str, timeout: int = 30,
                            max_output: int = 65536) -> CommandResult:
        """Run one bounded ``/bin/sh -c`` command and preserve its exit code."""
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        container = self.client.containers.get(cid)
        blocks = max(2, (max_output + 511) // 512)
        marker = '__LEO_COMMAND_RC__='
        wrapper = (
            'tmp=/tmp/leo-command-$$; '
            f'ulimit -f {blocks}; '
            f'timeout -s KILL {timeout} /bin/sh -c "$1" >"$tmp" 2>&1; '
            'rc=$?; head -c "$2" "$tmp" 2>/dev/null; '
            f'printf "\\n{marker}%s\\n" "$rc"; rm -f "$tmp"'
        )
        raw = container.exec_run(
            ['/bin/sh', '-c', wrapper, 'leo-command', cmd, str(max_output)])
        output = raw.output if isinstance(raw.output, bytes) else str(raw.output).encode()
        text = output.decode(errors='replace')
        marker_pos = text.rfind(marker)
        if marker_pos < 0:
            raise RuntimeError('Command wrapper did not return an exit code')
        body = text[:marker_pos].rstrip('\n').encode()
        code_text = text[marker_pos + len(marker):].strip().splitlines()[0]
        exit_code = int(code_text)
        return CommandResult(
            exit_code=exit_code,
            output=body,
            timed_out=exit_code in {124, 137},
            truncated=len(body) >= max_output,
        )

    def run_command(self, cid: str, command: list[str]) -> Any:
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        return self._exec(self.client.containers.get(cid), command)

    def run_probe(self, cid: str, command: list[str]) -> Any:
        """Run a read-only probe whose non-zero exit is an observed result."""
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        return self.client.containers.get(cid).exec_run(command)

    def start_background(self, cid: str, argv: list[str]) -> str:
        """Start a backend-generated argv in one owned container without a shell."""
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ValueError('Background command must be a non-empty argv list')
        handle = self.client.api.exec_create(cid, argv)['Id']
        self.client.api.exec_start(handle, detach=True)
        return str(handle)

    def stop_background(self, cid: str, handle: str) -> None:
        """Stop precisely the background exec represented by this Docker handle."""
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        inspect = self.client.api.exec_inspect(handle)
        pid = inspect.get('Pid')
        if not isinstance(pid, int) or pid <= 0:
            return
        self._exec(self.client.containers.get(cid), ['kill', str(pid)])

    def background_status(self, cid: str, handle: str) -> str:
        if cid not in self.owned:
            raise ValueError('Container is not owned by this session')
        status = self.client.api.exec_inspect(handle)
        if status.get('Running'):
            return 'running'
        return 'completed' if status.get('ExitCode') == 0 else 'failed'

    def create_link(self, source_name: str, target_name: str) -> dict[str, str]:
        src_cid = next((cid for cid, rec in self.owned.items() if rec['name'] == source_name), None)
        tgt_cid = next((cid for cid, rec in self.owned.items() if rec['name'] == target_name), None)
        if not src_cid or not tgt_cid:
            raise ValueError(f'One or both nodes not found: {source_name}, {target_name}')
        if source_name == target_name:
            raise ValueError('Self-links not supported')
        if any({record['source'], record['target']} == {source_name, target_name}
               for record in self.links):
            raise ValueError(f'Link already exists: {source_name}<->{target_name}')
        if self.subnet_counter >= 64:
            raise ValueError('Link subnet pool exhausted (maximum 64 links)')

        src_host = self.owned[src_cid]['host']
        tgt_host = self.owned[tgt_cid]['host']
        link = self.net.addLink(src_host, tgt_host)
        try:
            intf1, intf2 = link.intf1, link.intf2
            if intf1 is None or intf2 is None:
                raise RuntimeError('Containernet did not return both link interfaces')
            if intf1.node is src_host and intf2.node is tgt_host:
                src_intf, tgt_intf = intf1.name, intf2.name
            elif intf2.node is src_host and intf1.node is tgt_host:
                src_intf, tgt_intf = intf2.name, intf1.name
            else:
                raise RuntimeError('Containernet returned interfaces for unexpected endpoints')
            offset = self.subnet_counter * 4
            src_ip = f'192.168.0.{offset + 1}/30'
            tgt_ip = f'192.168.0.{offset + 2}/30'
            record = {'source': source_name, 'target': target_name,
                      'source_interface': src_intf, 'target_interface': tgt_intf,
                      'source_ip': src_ip, 'target_ip': tgt_ip}

            for cid, intf, ip in ((src_cid, src_intf, src_ip), (tgt_cid, tgt_intf, tgt_ip)):
                container = self.client.containers.get(cid)
                self._exec(container, ['ip', 'addr', 'add', ip, 'dev', intf])
                self._exec(container, ['ip', 'link', 'set', intf, 'up'])
                role = self.owned[cid]['role']
                forwarding = '1' if role in {'satellite', 'gateway'} else '0'
                self._exec(container, ['sysctl', '-w',
                                       f'net.ipv4.ip_forward={forwarding}'])

            self._update_routing(self.links + [record])
        except BaseException:
            link.delete()
            raise
        self.links.append(record)
        self.subnet_counter += 1
        return dict(record)

    def _update_routing(self, links: list[dict[str, str]] | None = None) -> None:
        from collections import deque
        active_links = self.links if links is None else links
        adj: dict[str, list[str]] = {rec['name']: [] for rec in self.owned.values()}
        for record in active_links:
            u, v = record['source'], record['target']
            adj[u].append(v)
            adj[v].append(u)

        for src in adj:
            for dst in adj:
                if src == dst:
                    continue
                queue = deque([[src]])
                visited = {src}
                path = None
                while queue:
                    curr_path = queue.popleft()
                    node = curr_path[-1]
                    if node == dst:
                        path = curr_path
                        break
                    for neighbor in adj[node]:
                        if neighbor not in visited:
                            visited.add(neighbor)
                            queue.append(curr_path + [neighbor])

                if path:
                    next_hop = path[1]
                    dst_service_ip = next((rec['service_ip'] for rec in self.owned.values()
                                           if rec['name'] == dst), None)
                    if dst_service_ip is None:
                        continue
                    active = next(record for record in active_links
                                  if {record['source'], record['target']} == {src, next_hop})
                    next_hop_ip = (active['target_ip'] if active['source'] == src
                                   else active['source_ip']).split('/')[0]
                    src_cid = next(cid for cid, rec in self.owned.items() if rec['name'] == src)
                    container = self.client.containers.get(src_cid)
                    self._exec(container, ['ip', 'route', 'replace', dst_service_ip + '/32',
                                           'via', next_hop_ip])

    def _remove(self, cid: str, host: Any) -> None:
        from docker.errors import NotFound
        self._close_terminals(cid)
        # terminate may fail or skip stopped containers; Docker ID cleanup is final.
        try:
            if host is not None:
                host.terminate()
        except Exception:
            # An incomplete Mininet host may lack attributes; still verify Docker.
            pass
        try:
            self.client.containers.get(cid).remove(force=True)
        except NotFound:
            pass
        try:
            self.client.containers.get(cid)
        except NotFound:
            self.owned.pop(cid, None)
        else:
            raise RuntimeError(f'Container still exists: {cid}')

    def close(self) -> None:
        errors = []
        # Mininet must stop links while its Docker host objects still exist.
        # Removing containers first makes net.stop() re-terminate stale hosts.
        try:
            self.net.stop()
        except Exception as exc:
            errors.append(f'network: {exc}')
        for cid, record in list(self.owned.items()):
            try:
                self._remove(cid, record['host'])
            except Exception as exc:
                errors.append(f'{cid}: {exc}')
        try:
            self.client.close()
        except Exception as exc:
            errors.append(f'docker client: {exc}')
        if errors:
            raise RuntimeError('Cleanup failed: ' + '; '.join(errors))
