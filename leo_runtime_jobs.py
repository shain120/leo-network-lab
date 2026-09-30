"""Persistent, shared runtime activity model for AI, Hypatia, and SNS-3."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:  # Runtime jobs are shared by the desktop process and worker/status processes.
    import fcntl
except ImportError:  # pragma: no cover - the supported desktop target is Linux.
    fcntl = None  # type: ignore[assignment]


TERMINAL_STATES = {'COMPLETED', 'FAILED', 'CANCELLED'}


class RuntimeJobStore:
    """Thread-safe runtime records with atomic, application-owned persistence."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            rows = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get('job_id'), str):
                self._jobs[row['job_id']] = row
        self._reconcile_processes()

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _reconcile_processes(self) -> None:
        changes: list[tuple[str, dict[str, Any]]] = []
        now = time.time()
        for job_id, job in self._jobs.items():
            if job.get('status') in TERMINAL_STATES:
                continue
            if self._pid_alive(job.get('pid')):
                changes.append((job_id, {
                    'status': 'RUNNING',
                    'message': 'Background process is still running.',
                }))
            elif job.get('pid'):
                changes.append((job_id, {
                    'status': 'WARNING',
                    'message': 'The previous process exited while the UI was closed; refresh artifacts to confirm completion.',
                    'completed_at': now,
                }))
        for job_id, fields in changes:
            self.update(job_id, **fields)

    @property
    def _lock_path(self) -> Path:
        return self.path.with_name(f'{self.path.name}.lock')

    @contextmanager
    def _disk_lock(self):
        """Serialize read-modify-write cycles across all local Python processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open('a+', encoding='utf-8') as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_disk_jobs(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            rows = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return {
            row['job_id']: dict(row) for row in rows if isinstance(row, dict)
            and isinstance(row.get('job_id'), str)
        } if isinstance(rows, list) else {}

    def _write_disk_jobs_locked(self, jobs: dict[str, dict[str, Any]]) -> None:
        """Atomically persist while the caller holds ``_disk_lock``.

        A unique temporary path is essential: ``Path.replace`` removes its source,
        so two writers sharing ``runtime-jobs.tmp`` caused the reported ENOENT.
        """
        temporary = self.path.with_name(
            f'.{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp'
        )
        try:
            with temporary.open('w', encoding='utf-8') as handle:
                json.dump(list(jobs.values()), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            # ``os.replace`` already removed it on success; this covers write errors.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _save(self) -> None:
        with self._disk_lock():
            disk_jobs = self._read_disk_jobs()
            # Preserve records created by another process after this store was loaded.
            disk_jobs.update(self._jobs)
            self._jobs = disk_jobs
            self._write_disk_jobs_locked(self._jobs)

    def create(self, *, system: str, job_type: str, status: str = 'QUEUED', stage: str = 'QUEUED',
               job_id: str | None = None, progress_stages: list[dict[str, Any]] | None = None,
               **fields: Any) -> dict[str, Any]:
        now = time.time()
        record = {
            'job_id': job_id or uuid.uuid4().hex, 'system': system.upper(), 'type': job_type,
            'status': status.upper(), 'stage': stage, 'started_at': now,
            'completed_at': now if status.upper() in TERMINAL_STATES else None,
            'pid': None, 'progress_stages': progress_stages or [],
            'requested_threads': None, 'resolved_threads': None, 'cpu_count': None,
            'message': '', 'log_path': None, 'error': None,
        }
        record.update(fields)
        with self._lock:
            with self._disk_lock():
                disk_jobs = self._read_disk_jobs()
                existing = disk_jobs.get(record['job_id'], self._jobs.get(record['job_id'], {}))
                merged = dict(existing)
                merged.update(record)
                disk_jobs[record['job_id']] = merged
                self._jobs = disk_jobs
                self._write_disk_jobs_locked(disk_jobs)
        return dict(merged)

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            if 'status' in fields:
                fields['status'] = str(fields['status']).upper()
                if fields['status'] in TERMINAL_STATES:
                    fields.setdefault('completed_at', time.time())
            with self._disk_lock():
                disk_jobs = self._read_disk_jobs()
                existing = disk_jobs.get(job_id, self._jobs.get(job_id))
                if existing is None:
                    raise KeyError(job_id)
                merged = dict(existing)
                merged.update(fields)
                disk_jobs[job_id] = merged
                self._jobs = disk_jobs
                self._write_disk_jobs_locked(disk_jobs)
                return dict(merged)

    def upsert_external(self, record: dict[str, Any], *, system: str = 'HYPATIA') -> dict[str, Any]:
        job_id = str(record['job_id'])
        with self._lock:
            if job_id not in self._jobs:
                # Another process may have created the row since this store loaded.
                # Refresh before creating a default record, so its timing/job metadata
                # is retained instead of being reset by a status poll.
                with self._disk_lock():
                    disk_jobs = self._read_disk_jobs()
                    if job_id in disk_jobs:
                        self._jobs = disk_jobs
            if job_id not in self._jobs:
                self.create(system=system, job_type='hypatia_job', job_id=job_id,
                            status=record.get('status', 'QUEUED'), stage=record.get('stage', 'QUEUED'))
            allowed = {key: record.get(key) for key in (
                'status', 'stage', 'pid', 'requested_threads', 'resolved_threads', 'cpu_count',
                'message', 'error', 'exit_code', 'dataset_id', 'analysis_id', 'command',
                'stdout_log', 'stderr_log', 'elapsed_sec', 'progress_stages',
                'duration_s', 'time_step_ms', 'dataset_path', 'dynamic_state_path',
                'process_alive', 'cpu_percent', 'memory_percent', 'memory_rss_bytes',
                'generated_states', 'total_states', 'progress_percent',
                'last_progress_at_monotonic', 'last_progress_age_sec', 'warning') if key in record}
            if record.get('stdout_log'):
                allowed['log_path'] = record['stdout_log']
            return self.update(job_id, **allowed)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._jobs.get(job_id)
            return self._with_elapsed(row) if row else None

    def list(self, *, system: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [self._with_elapsed(row) for row in self._jobs.values()
                    if system is None or row.get('system') == system.upper()]
        return sorted(rows, key=lambda row: float(row.get('started_at') or 0), reverse=True)

    def latest(self, system: str) -> dict[str, Any] | None:
        rows = self.list(system=system)
        return rows[0] if rows else None

    @staticmethod
    def _with_elapsed(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        started = float(result.get('started_at') or time.time())
        ended = float(result.get('completed_at') or time.time())
        result['elapsed_sec'] = max(0.0, ended - started)
        return result
