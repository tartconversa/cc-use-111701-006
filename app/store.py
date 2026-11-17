"""仅追加的接收台账：原件内容寻址 + JSONL 事件日志。

保证：
* 同伙伴同 ISA 控制号、字节相同 → 重投返回首次接收结论，不产生第二份台账（幂等）。
* 同伙伴同 ISA 控制号、字节不同 → 冲突拒收并留痕，绝不覆盖既有结论。
* 控制号按伙伴隔离，不同伙伴重用同一号码互不影响。
* 被接受的修订单（头部 REF*CR 指向原交易集键）形成版本链；当前视图只取链头，
  整单替换而非逐行相加，链上所有版本仍可追溯。
* 写盘走临时文件 + fsync + 原子改名，事件日志在文件锁内追加；进程重启后重放日志，
  接收结论与重启前一致。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .activity import SetContent
from .x12 import Interchange

JOURNAL_VERSION = 1
_EVENT_LOG = "events.log"
_LOCK_FILE = "store.lock"
_RAW_DIR = "raw"
_RECEIPT_DIR = "receipts"
_NAME_NAMESPACE = uuid.UUID("6f9619ff-8b86-c006-b42e-935c1d2a0001")


def set_key(partner_id: str, isa_control: str, gs_control: str, st_control: str) -> str:
    return f"{partner_id}::{isa_control}/{gs_control}/{st_control}"


def parse_correction_ref(partner_id: str, isa_control: str, ref: str) -> str:
    """REF*CR 形如 ``ISA控制号/GS控制号/ST控制号``，解析为原交易集键。"""
    parts = ref.split("/")
    if len(parts) != 3 or not all(parts):
        return ""
    return set_key(partner_id, parts[0], parts[1], parts[2])


@dataclass(frozen=True)
class StoredSet:
    key: str
    group_control: str
    set_type: str
    control: str
    status: str
    reason_codes: tuple[str, ...]
    activities: tuple[dict, ...]
    correction_ref: str
    supersedes: str
    warnings: tuple[dict, ...]
    received_at: str
    interchange_sha256: str


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    # 目录项也需落盘，重启后改名才保证可见。
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class Store:
    """单伙伴的接收台账；通过 :meth:`locked` 持有文件锁后使用。"""

    def __init__(self, root: str | os.PathLike, partner_id: str):
        if not partner_id or not isinstance(partner_id, str):
            raise ValueError("伙伴身份无效：控制号必须按伙伴隔离")
        self.root = Path(root)
        self.partner_id = partner_id
        self._lock_handle = None
        self._by_hash: dict[str, dict] = {}          # 交换哈希 → 首次 INGESTED 事件
        self._control_index: dict[str, str] = {}     # 伙伴内 ISA 控制号 → 交换哈希
        self._sets: dict[str, StoredSet] = {}
        self._chain_supersedes: dict[str, str] = {}  # 新单键 → 原单键
        self._chain_heads: dict[str, str] = {}       # 原单键 → 当前链头键

    # ---- 生命周期 ----------------------------------------------------------

    def locked(self):
        return _StoreLock(self)

    def _open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / _RAW_DIR).mkdir(exist_ok=True)
        (self.root / _RECEIPT_DIR).mkdir(exist_ok=True)
        self._lock_handle = open(self.root / _LOCK_FILE, "a+")
        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
        self._replay()

    def close(self) -> None:
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def _replay(self) -> None:
        log = self.root / _EVENT_LOG
        if not log.exists():
            return
        with open(log, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"事件日志第 {line_no} 行损坏，拒绝在不确定状态下继续") from exc
                self._apply(event)

    def _apply(self, event: dict) -> None:
        if event.get("partner_id") != self.partner_id:
            return  # 目录按伙伴隔离，遇到他人事件只跳过
        kind = event["event"]
        if kind == "INGESTED":
            digest = event["interchange"]["sha256"]
            self._by_hash.setdefault(digest, event)
            self._control_index.setdefault(event["interchange"]["control"], digest)
            for raw_set in event["sets"]:
                key = raw_set["key"]
                stored = StoredSet(
                    key=key,
                    group_control=raw_set["group_control"],
                    set_type=raw_set["set_type"],
                    control=raw_set["control"],
                    status=raw_set["status"],
                    reason_codes=tuple(raw_set["reason_codes"]),
                    activities=tuple(raw_set["activities"]),
                    correction_ref=raw_set.get("correction_ref", ""),
                    supersedes=raw_set.get("supersedes", ""),
                    warnings=tuple(raw_set.get("warnings", ())),
                    received_at=event["received_at"],
                    interchange_sha256=digest,
                )
                self._sets[key] = stored
                if stored.status == "ACCEPTED" and stored.supersedes:
                    self._chain_supersedes[key] = stored.supersedes
                    head = key
                    # 原单可能本身也是修订单，沿链找到最早的根并把根的链头更新过来。
                    root = stored.supersedes
                    while root in self._chain_supersedes:
                        root = self._chain_supersedes[root]
                    self._chain_heads[root] = head
        # CONFLICT 事件仅作审计留痕，不改变任何既有结论。

    # ---- 写入 --------------------------------------------------------------

    def record_unparseable(self, raw: bytes, error_message: str, offset: int) -> dict:
        """ISA 都无法识别的传输：内容寻址留痕，同字节重投结论一致。"""
        digest = hashlib.sha256(raw).hexdigest()
        previous = self.root / _RAW_DIR / f"{digest}.x12"
        replay = previous.exists()
        if not replay:
            _atomic_write(previous, raw)
        event = {
            "event": "UNPARSEABLE",
            "version": JOURNAL_VERSION,
            "partner_id": self.partner_id,
            "received_at": _now(),
            "raw_sha256": digest,
            "offset": offset,
            "reason": "INTERCHANGE_UNPARSEABLE",
            "message": error_message,
        }
        self._append_event(event)
        return {
            "receipt_id": "",
            "partner_id": self.partner_id,
            "interchange_control": "",
            "raw_sha256": digest,
            "disposition": "REJECTED_INTERCHANGE",
            "replay": replay,
            "reason": "INTERCHANGE_UNPARSEABLE",
            "message": error_message,
            "sets": [],
            "acknowledgments": [],
        }

    def _append_event(self, event: dict) -> None:
        log = self.root / _EVENT_LOG
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def record_ingest(self, interchange: Interchange, stored_sets: list[StoredSet],
                      acknowledgments: list[dict]) -> dict:
        digest = interchange.raw_sha256
        # 幂等：同字节重投直接回放首次结论。
        if digest in self._by_hash:
            return self._receipt_from_event(self._by_hash[digest], replay=True)
        # 同伙伴同控制号但字节不同：冲突，绝不覆盖。
        existing = self._control_index.get(interchange.control)
        if existing is not None and existing != digest:
            event = {
                "event": "CONFLICT",
                "version": JOURNAL_VERSION,
                "partner_id": self.partner_id,
                "received_at": _now(),
                "interchange_control": interchange.control,
                "incoming_sha256": digest,
                "existing_sha256": existing,
                "reason": "DUPLICATE_CONTROL_DIFFERENT_BYTES",
            }
            self._append_event(event)
            return {
                "receipt_id": "",
                "partner_id": self.partner_id,
                "interchange_control": interchange.control,
                "raw_sha256": digest,
                "disposition": "REJECTED_INTERCHANGE",
                "replay": False,
                "reason": "DUPLICATE_CONTROL_DIFFERENT_BYTES",
                "existing_raw_sha256": existing,
                "sets": [],
                "acknowledgments": [],
            }

        # 原件内容寻址落盘（同哈希天然去重，且不改写已有文件）。
        _atomic_write(self.root / _RAW_DIR / f"{digest}.x12", interchange.raw)

        receipt_id = "urn:uuid:" + str(uuid.uuid5(
            _NAME_NAMESPACE, f"receipt:{self.partner_id}:{digest}"))
        event = {
            "event": "INGESTED",
            "version": JOURNAL_VERSION,
            "partner_id": self.partner_id,
            "received_at": _now(),
            "receipt_id": receipt_id,
            "interchange": {
                "sha256": digest,
                "control": interchange.control,
                "sender_qualifier": interchange.sender_qual,
                "sender_id": interchange.sender_id,
                "receiver_qualifier": interchange.receiver_qual,
                "receiver_id": interchange.receiver_id,
                "date": interchange.date,
                "time": interchange.time,
                "delimiters": {
                    "element": interchange.element_sep,
                    "component": interchange.component_sep,
                    "segment": interchange.segment_term,
                },
                "group_count": len(interchange.groups),
                "set_count": sum(len(g.sets) for g in interchange.groups),
                "envelope_errors": [e.__dict__ for e in interchange.errors],
            },
            "sets": [
                {
                    "key": s.key,
                    "group_control": s.group_control,
                    "set_type": s.set_type,
                    "control": s.control,
                    "status": s.status,
                    "reason_codes": list(s.reason_codes),
                    "activities": list(s.activities),
                    "correction_ref": s.correction_ref,
                    "supersedes": s.supersedes,
                    "warnings": list(s.warnings),
                }
                for s in stored_sets
            ],
            "acknowledgments": acknowledgments,
        }
        self._append_event(event)
        self._apply(event)
        receipt = self._receipt_from_event(event, replay=False)
        _atomic_write(
            self.root / _RECEIPT_DIR / f"{receipt_id.split(':', 2)[-1]}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            .encode("utf-8"),
        )
        return receipt

    def resolve_correction(self, interchange: Interchange, content: SetContent) -> tuple[str, str]:
        """返回 (supersedes 键, 拒绝原因)；非修订单返回 ("", "")。"""
        if not content.correction_ref:
            return "", ""
        original_key = parse_correction_ref(
            self.partner_id, interchange.control, content.correction_ref)
        if not original_key:
            return "", "CORRECTION_REF_MALFORMED"
        original = self._sets.get(original_key)
        if original is None:
            return "", "ORIGINAL_NOT_FOUND"
        if original.status != "ACCEPTED":
            return "", "ORIGINAL_WAS_REJECTED"
        return original_key, ""

    # ---- 读取 --------------------------------------------------------------

    def get_set(self, key: str) -> StoredSet | None:
        return self._sets.get(key)

    def lineage(self, key: str) -> list[str]:
        """返回从最早版本到当前链头的交易集键序列。"""
        root = key
        while root in self._chain_supersedes:
            root = self._chain_supersedes[root]
        chain = [root]
        head = self._chain_heads.get(root, root)
        cursor = head
        reverse = []
        while cursor != root:
            reverse.append(cursor)
            cursor = self._chain_supersedes[cursor]
        chain.extend(reversed(reverse))
        return chain

    def current_quantities(self) -> list[dict]:
        """当前有效数量：只返回版本链链头的接受条目，被取代版本不参与、不相加。"""
        head_keys = set()
        for key, stored in self._sets.items():
            if stored.status != "ACCEPTED":
                continue
            root = key
            while root in self._chain_supersedes:
                root = self._chain_supersedes[root]
            head_keys.add(self._chain_heads.get(root, root))
        result = []
        for key in sorted(head_keys):
            stored = self._sets[key]
            for activity in stored.activities:
                row = dict(activity)
                row["set_key"] = key
                row["received_at"] = stored.received_at
                result.append(row)
        return result

    def _receipt_from_event(self, event: dict, *, replay: bool) -> dict:
        return {
            "receipt_id": event["receipt_id"],
            "partner_id": event["partner_id"],
            "interchange_control": event["interchange"]["control"],
            "raw_sha256": event["interchange"]["sha256"],
            "disposition": "PROCESSED",
            "replay": replay,
            "received_at": event["received_at"],
            "envelope": {
                "group_count": event["interchange"]["group_count"],
                "set_count": event["interchange"]["set_count"],
                "errors": event["interchange"]["envelope_errors"],
            },
            "sets": [
                {
                    "set_key": s["key"],
                    "group_control": s["group_control"],
                    "set_type": s["set_type"],
                    "control": s["control"],
                    "status": s["status"],
                    "reason_codes": s["reason_codes"],
                    "activity_count": len(s["activities"]),
                    "correction_ref": s.get("correction_ref", ""),
                    "supersedes": s.get("supersedes", ""),
                    "warnings": s.get("warnings", []),
                }
                for s in event["sets"]
            ],
            "acknowledgments": event["acknowledgments"],
        }


class _StoreLock:
    def __init__(self, store: Store):
        self._store = store

    def __enter__(self) -> Store:
        self._store._open()
        return self._store

    def __exit__(self, *exc) -> None:
        self._store.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
