"""定时调度模块 - 任务模型、持久化存储、批次锁、调度器."""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Any, Callable

import yaml

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

try:
    import msvcrt
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False

from .models import Batch
from .storage import BatchStorage
from .audit import AuditStorage
from .matcher import run_matching
from .report import generate_summary
from .rules import load_rules, RuleValidationError


class ScheduleStep(str, Enum):
    """调度步骤枚举."""
    IMPORT = "import"
    MATCH = "match"
    REPORT = "report"


class ScheduleStatus(str, Enum):
    """调度任务状态."""
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    EXPIRED = "expired"


class ScheduleRunStatus(str, Enum):
    """单次运行状态."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED_LOCK = "skipped_lock"


@dataclass
class ScheduleImportConfig:
    """import 步骤的配置."""
    file_type: str
    file_path: str
    col_map: Optional[Dict[str, str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_type": self.file_type,
            "file_path": self.file_path,
            "col_map": self.col_map,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleImportConfig":
        return cls(
            file_type=data["file_type"],
            file_path=data["file_path"],
            col_map=data.get("col_map"),
        )


@dataclass
class ScheduleReportConfig:
    """report 步骤的配置."""
    output_path: str
    with_summary: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_path": self.output_path,
            "with_summary": self.with_summary,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleReportConfig":
        return cls(
            output_path=data["output_path"],
            with_summary=data.get("with_summary", False),
        )


@dataclass
class ScheduleTask:
    """定时任务定义."""
    task_id: str
    name: str
    batch_id: str
    cron: str
    steps: List[ScheduleStep]
    import_configs: List[ScheduleImportConfig] = field(default_factory=list)
    rule_file: Optional[str] = None
    report_config: Optional[ScheduleReportConfig] = None
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    expires_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_run_status: Optional[ScheduleRunStatus] = None
    retry_count: int = 0
    max_retries: int = 3

    @classmethod
    def create(
        cls,
        name: str,
        batch_id: str,
        cron: str,
        steps: List[ScheduleStep],
        import_configs: Optional[List[ScheduleImportConfig]] = None,
        rule_file: Optional[str] = None,
        report_config: Optional[ScheduleReportConfig] = None,
        expires_at: Optional[str] = None,
        max_retries: int = 3,
    ) -> "ScheduleTask":
        return cls(
            task_id="SCHED-" + uuid.uuid4().hex[:8].upper(),
            name=name,
            batch_id=batch_id,
            cron=cron,
            steps=steps,
            import_configs=import_configs or [],
            rule_file=rule_file,
            report_config=report_config,
            expires_at=expires_at,
            max_retries=max_retries,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "batch_id": self.batch_id,
            "cron": self.cron,
            "steps": [s.value for s in self.steps],
            "import_configs": [c.to_dict() for c in self.import_configs],
            "rule_file": self.rule_file,
            "report_config": self.report_config.to_dict() if self.report_config else None,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "last_run_at": self.last_run_at,
            "last_run_status": self.last_run_status.value if self.last_run_status else None,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleTask":
        return cls(
            task_id=data["task_id"],
            name=data["name"],
            batch_id=data["batch_id"],
            cron=data["cron"],
            steps=[ScheduleStep(s) for s in data.get("steps", [])],
            import_configs=[ScheduleImportConfig.from_dict(c) for c in data.get("import_configs", [])],
            rule_file=data.get("rule_file"),
            report_config=ScheduleReportConfig.from_dict(data["report_config"]) if data.get("report_config") else None,
            status=ScheduleStatus(data.get("status", ScheduleStatus.ACTIVE.value)),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            expires_at=data.get("expires_at"),
            last_run_at=data.get("last_run_at"),
            last_run_status=ScheduleRunStatus(data["last_run_status"]) if data.get("last_run_status") else None,
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
        )

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return datetime.fromisoformat(self.expires_at) <= datetime.now()
        except (ValueError, TypeError):
            return False

    def should_run_now(self, now: Optional[datetime] = None) -> bool:
        """简化的 cron 检查 - 支持 HH:MM 格式（每天该时间触发）或 'every N minutes'."""
        if self.status != ScheduleStatus.ACTIVE:
            return False
        if self.is_expired():
            return False
        now = now or datetime.now()
        cron = self.cron.strip()

        if cron.startswith("every ") and cron.endswith(" minutes"):
            try:
                interval = int(cron.split()[1])
                if not self.last_run_at:
                    return True
                last = datetime.fromisoformat(self.last_run_at)
                return (now - last) >= timedelta(minutes=interval)
            except (ValueError, IndexError):
                return False

        if ":" in cron and len(cron.split(":")) == 2:
            try:
                hh, mm = cron.split(":")
                target_hour = int(hh)
                target_minute = int(mm)
                if now.hour != target_hour or now.minute != target_minute:
                    return False
                if not self.last_run_at:
                    return True
                last = datetime.fromisoformat(self.last_run_at)
                last_date = last.date()
                today = now.date()
                return last_date < today
            except ValueError:
                return False

        return False

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()


class ScheduleStorage:
    """调度任务持久化存储 - 基于 YAML 文件."""

    SCHEDULE_DIRNAME = "schedules"
    LOCK_DIRNAME = "locks"

    def __init__(self, storage_dir: str):
        self.storage_dir = storage_dir
        self.schedules_dir = os.path.join(storage_dir, self.SCHEDULE_DIRNAME)
        self.locks_dir = os.path.join(storage_dir, self.LOCK_DIRNAME)
        os.makedirs(self.schedules_dir, exist_ok=True)
        os.makedirs(self.locks_dir, exist_ok=True)

    def _task_path(self, task_id: str) -> str:
        return os.path.join(self.schedules_dir, f"{task_id}.yaml")

    def save(self, task: ScheduleTask) -> None:
        task.touch()
        path = self._task_path(task.task_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(task.to_dict(), f, allow_unicode=True, default_flow_style=False)
        os.replace(tmp, path)

    def load(self, task_id: str) -> ScheduleTask:
        path = self._task_path(task_id)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"调度任务不存在: {task_id}")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return ScheduleTask.from_dict(data)

    def delete(self, task_id: str) -> bool:
        path = self._task_path(task_id)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def list_tasks(self) -> List[Dict[str, Any]]:
        """列出所有任务的摘要信息."""
        results = []
        if not os.path.isdir(self.schedules_dir):
            return results
        for fname in sorted(os.listdir(self.schedules_dir)):
            if not fname.endswith(".yaml"):
                continue
            path = os.path.join(self.schedules_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                results.append({
                    "task_id": data.get("task_id"),
                    "name": data.get("name"),
                    "batch_id": data.get("batch_id"),
                    "cron": data.get("cron"),
                    "steps": data.get("steps", []),
                    "status": data.get("status", ScheduleStatus.ACTIVE.value),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "expires_at": data.get("expires_at"),
                    "last_run_at": data.get("last_run_at"),
                    "last_run_status": data.get("last_run_status"),
                    "retry_count": data.get("retry_count", 0),
                })
            except (yaml.YAMLError, KeyError, OSError):
                continue
        return results

    def load_all_active(self) -> List[ScheduleTask]:
        """加载所有活跃且未过期的任务."""
        tasks: List[ScheduleTask] = []
        if not os.path.isdir(self.schedules_dir):
            return tasks
        for fname in sorted(os.listdir(self.schedules_dir)):
            if not fname.endswith(".yaml"):
                continue
            path = os.path.join(self.schedules_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                task = ScheduleTask.from_dict(data)
                if task.status == ScheduleStatus.ACTIVE and not task.is_expired():
                    tasks.append(task)
                elif task.is_expired() and task.status == ScheduleStatus.ACTIVE:
                    task.status = ScheduleStatus.EXPIRED
                    self.save(task)
            except (yaml.YAMLError, KeyError, OSError, ValueError):
                continue
        return tasks

    def task_exists(self, task_id: str) -> bool:
        return os.path.isfile(self._task_path(task_id))


class BatchLock:
    """跨平台的批次级互斥锁（基于文件锁，Unix 用 fcntl，Windows 用 msvcrt，退化为 PID 文件锁）."""

    def __init__(self, storage_dir: str, batch_id: str):
        locks_dir = os.path.join(storage_dir, ScheduleStorage.LOCK_DIRNAME)
        os.makedirs(locks_dir, exist_ok=True)
        self.lock_file = os.path.join(locks_dir, f"batch_{batch_id}.lock")
        self._fh: Optional[Any] = None
        self._locked = False

    def _try_lock_fcntl(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    return False
                time.sleep(0.1)

    def _try_lock_msvcrt(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        self._fh.write(" ")
        self._fh.flush()
        while True:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except (OSError, IOError):
                if time.time() >= deadline:
                    return False
                time.sleep(0.1)

    def _try_lock_fallback(self, timeout: float) -> bool:
        """基于 PID 的文件锁回退方案（非原子，但足够 CLI 单进程场景）."""
        deadline = time.time() + timeout
        while True:
            try:
                if os.path.isfile(self.lock_file):
                    try:
                        with open(self.lock_file, "r") as f:
                            content = f.read().strip()
                        pid_str = None
                        for line in content.split("\n"):
                            if line.startswith("pid="):
                                pid_str = line.split("=", 1)[1].strip()
                                break
                        if pid_str:
                            try:
                                pid = int(pid_str)
                                if _pid_alive(pid):
                                    if time.time() >= deadline:
                                        return False
                                    time.sleep(0.1)
                                    continue
                            except ValueError:
                                pass
                    except (OSError, IOError):
                        pass
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(f"pid={os.getpid()} ts={datetime.now().isoformat()}\n")
                return True
            except FileExistsError:
                if time.time() >= deadline:
                    return False
                time.sleep(0.1)
            except OSError:
                if time.time() >= deadline:
                    return False
                time.sleep(0.1)

    def acquire(self, timeout: float = 0) -> bool:
        """尝试获取锁。timeout=0 表示非阻塞."""
        if self._locked:
            return True
        try:
            self._fh = open(self.lock_file, "w")
            if _HAS_FCNTL:
                if not self._try_lock_fcntl(timeout):
                    self._fh.close()
                    self._fh = None
                    return False
            elif _HAS_MSVCRT:
                if not self._try_lock_msvcrt(timeout):
                    self._fh.close()
                    self._fh = None
                    return False
            else:
                self._fh.close()
                self._fh = None
                if not self._try_lock_fallback(timeout):
                    return False
                self._fh = open(self.lock_file, "a")
            try:
                self._fh.write(f"pid={os.getpid()} ts={datetime.now().isoformat()}\n")
                self._fh.flush()
            except Exception:
                pass
            self._locked = True
            return True
        except OSError:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            return False

    def release(self) -> None:
        if not self._locked:
            return
        try:
            if self._fh:
                if _HAS_FCNTL:
                    try:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
                elif _HAS_MSVCRT:
                    try:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        try:
                            self._fh.seek(0)
                            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        finally:
            self._locked = False
            try:
                if os.path.isfile(self.lock_file):
                    os.remove(self.lock_file)
            except Exception:
                pass

    def __enter__(self) -> "BatchLock":
        if not self.acquire(timeout=0):
            raise OSError(f"Failed to acquire batch lock: {self.lock_file}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    @property
    def locked(self) -> bool:
        return self._locked


def _pid_alive(pid: int) -> bool:
    """跨平台检查进程是否存活."""
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_INFORMATION = 0x0400
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError, OSError):
        return False
    except Exception:
        return False


class TaskExecutor:
    """任务执行器 - 负责实际运行 import/match/report 步骤."""

    def __init__(self, batch_storage: BatchStorage, audit: AuditStorage):
        self.batch_storage = batch_storage
        self.audit = audit

    def execute(self, task: ScheduleTask) -> Dict[str, Any]:
        """执行任务，返回执行结果摘要."""
        from .parser import parse_file, FileType, ParseResult
        from .report import export_discrepancies_csv, export_summary_csv

        results: Dict[str, Any] = {
            "task_id": task.task_id,
            "batch_id": task.batch_id,
            "steps": {},
            "success": True,
            "error": None,
        }

        try:
            batch = self.batch_storage.load(task.batch_id)
        except FileNotFoundError as e:
            results["success"] = False
            results["error"] = f"批次不存在: {e}"
            self.audit.log(
                "schedule_run", task.task_id, 0,
                f"任务 {task.name} 执行失败: 批次 {task.batch_id} 不存在",
            )
            return results

        for step in task.steps:
            try:
                if step == ScheduleStep.IMPORT:
                    step_result = self._run_import(task, batch)
                    results["steps"]["import"] = step_result
                elif step == ScheduleStep.MATCH:
                    step_result = self._run_match(task, batch)
                    results["steps"]["match"] = step_result
                elif step == ScheduleStep.REPORT:
                    step_result = self._run_report(task, batch)
                    results["steps"]["report"] = step_result
            except Exception as e:
                results["success"] = False
                results["error"] = f"步骤 {step.value} 执行失败: {e}"
                self.audit.log(
                    "schedule_run", task.task_id, 0,
                    f"任务 {task.name} 步骤 {step.value} 失败: {e}",
                )
                return results

        summary_parts = []
        for step_name, sr in results["steps"].items():
            summary_parts.append(f"{step_name}({sr.get('count', 0)})")
        self.audit.log(
            "schedule_run", task.task_id,
            sum(sr.get("count", 0) for sr in results["steps"].values()),
            f"任务 {task.name} 执行完成: " + ", ".join(summary_parts) if summary_parts else f"任务 {task.name} 执行完成",
        )
        return results

    def _run_import(self, task: ScheduleTask, batch: Batch) -> Dict[str, Any]:
        from .parser import parse_file, FileType

        total_count = 0
        imported_files = []
        type_map = {
            "bank": FileType.BANK_STATEMENT,
            "system": FileType.SYSTEM_RECEIPT,
            "adjustment": FileType.MANUAL_ADJUSTMENT,
        }

        for cfg in task.import_configs:
            ft = type_map.get(cfg.file_type)
            if not ft:
                raise ValueError(f"未知文件类型: {cfg.file_type}")
            result, imported = parse_file(
                cfg.file_path, ft,
                storage_dir=self.batch_storage.storage_dir,
                extra_col_map=cfg.col_map,
            )
            if ft == FileType.BANK_STATEMENT:
                batch.bank_txns = result.transactions
            elif ft == FileType.SYSTEM_RECEIPT:
                batch.system_txns = result.transactions
            else:
                batch.adjustment_txns = result.transactions

            existing_names = {f.file_type for f in batch.imported_files}
            if ft in existing_names:
                batch.imported_files = [f for f in batch.imported_files if f.file_type != ft]
            batch.imported_files.append(imported)
            total_count += result.row_count
            imported_files.append({
                "type": cfg.file_type,
                "path": cfg.file_path,
                "count": result.row_count,
                "errors": len(result.errors),
            })

        self.batch_storage.save(batch)
        return {"count": total_count, "files": imported_files}

    def _run_match(self, task: ScheduleTask, batch: Batch) -> Dict[str, Any]:
        rule_path = task.rule_file or batch.rule_file
        try:
            rules_obj = load_rules(rule_path)
        except RuleValidationError as e:
            raise ValueError(f"规则文件错误: {e}")

        discrepancies = run_matching(batch, rules_obj)
        batch.discrepancies = discrepancies
        if task.rule_file:
            batch.rule_file = os.path.abspath(task.rule_file)
        self.batch_storage.save(batch)
        return {"count": len(discrepancies)}

    def _run_report(self, task: ScheduleTask, batch: Batch) -> Dict[str, Any]:
        from .report import export_discrepancies_csv, export_summary_csv

        if not task.report_config:
            return {"count": 0, "skipped": "no report config"}

        count = export_discrepancies_csv(batch, task.report_config.output_path)
        self.batch_storage.record_export(batch, task.report_config.output_path, "discrepancies")

        if task.report_config.with_summary:
            base, ext = os.path.splitext(task.report_config.output_path)
            summary_path = f"{base}_summary{ext}"
            export_summary_csv(batch, summary_path)

        return {"count": count, "output": task.report_config.output_path}


class Scheduler:
    """调度器 - 管理任务加载、轮询触发、并发控制."""

    def __init__(self, storage_dir: str):
        self.storage_dir = storage_dir
        self.schedule_storage = ScheduleStorage(storage_dir)
        self.batch_storage = BatchStorage(storage_dir)
        self.audit = AuditStorage(storage_dir)
        self.executor = TaskExecutor(self.batch_storage, self.audit)
        self._tasks: Dict[str, ScheduleTask] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def load_active_tasks(self) -> int:
        """加载所有活跃未过期的任务（启动时调用）."""
        tasks = self.schedule_storage.load_all_active()
        with self._lock:
            self._tasks = {t.task_id: t for t in tasks}
        self.audit.log(
            "schedule_load", "system", len(tasks),
            f"调度器启动，加载 {len(tasks)} 个活跃任务",
        )
        return len(tasks)

    def run_task_now(self, task_id: str) -> Dict[str, Any]:
        """立即手动触发一个任务."""
        if not self.schedule_storage.task_exists(task_id):
            raise FileNotFoundError(f"调度任务不存在: {task_id}")
        task = self.schedule_storage.load(task_id)
        return self._execute_task(task, manual=True)

    def _execute_task(self, task: ScheduleTask, manual: bool = False) -> Dict[str, Any]:
        """执行单个任务（含批次锁控制）."""
        lock = BatchLock(self.storage_dir, task.batch_id)
        if not lock.acquire(timeout=0):
            task.last_run_at = datetime.now().isoformat()
            task.last_run_status = ScheduleRunStatus.SKIPPED_LOCK
            self.schedule_storage.save(task)
            self.audit.log(
                "schedule_run", task.task_id, 0,
                f"任务 {task.name} 跳过: 批次 {task.batch_id} 被锁定，冲突互斥",
            )
            return {
                "task_id": task.task_id,
                "success": False,
                "skipped": True,
                "reason": "batch_locked",
                "error": f"批次 {task.batch_id} 被其他任务锁定",
            }

        try:
            task.last_run_at = datetime.now().isoformat()
            task.last_run_status = ScheduleRunStatus.RUNNING
            self.schedule_storage.save(task)

            result = self.executor.execute(task)

            if result["success"]:
                task.last_run_status = ScheduleRunStatus.SUCCESS
                task.retry_count = 0
            else:
                task.retry_count += 1
                if task.retry_count >= task.max_retries:
                    task.last_run_status = ScheduleRunStatus.FAILED
                    self.audit.log(
                        "schedule_run", task.task_id, 0,
                        f"任务 {task.name} 达到最大重试次数({task.max_retries})，标记为失败",
                    )
                else:
                    task.last_run_status = ScheduleRunStatus.FAILED
            self.schedule_storage.save(task)
            return result
        finally:
            lock.release()

    def start(self, poll_interval: int = 30) -> None:
        """启动调度器后台线程."""
        self._stop_event.clear()
        self.load_active_tasks()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(poll_interval,),
            daemon=True,
            name="scheduler-loop",
        )
        self._thread.start()

    def stop(self) -> None:
        """停止调度器."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run_loop(self, poll_interval: int) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                self.audit.log(
                    "schedule_error", "system", 0,
                    f"调度器 tick 异常: {e}",
                )
            self._stop_event.wait(poll_interval)

    def _tick(self) -> None:
        now = datetime.now()
        with self._lock:
            tasks = list(self._tasks.values())

        for task in tasks:
            if task.should_run_now(now):
                try:
                    self._execute_task(task)
                except Exception as e:
                    self.audit.log(
                        "schedule_run", task.task_id, 0,
                        f"任务 {task.name} 异常: {e}",
                    )
