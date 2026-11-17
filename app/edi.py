"""接收编排：X12 原文 → 信封解析 → 852 提取 → 997 确认 → 持久台账与可核对收据。

一个交易集的错误只影响该交易集：同组兄弟单据照常接受，AK5 与收据逐项反映状态。
伙伴身份来自传输配置（调用方传入），不用控制号代替身份。
"""
from __future__ import annotations

import hashlib

from . import ack997
from .activity import extract_set
from .store import StoredSet, Store, set_key
from .x12 import Interchange, InterchangeUnparseable, SetNode, parse


def _set_outcomes(interchange: Interchange, stored_sets: list[StoredSet]) -> dict[str, list[dict]]:
    """按组聚合 997 的 AK2/AK5 所需结果，AK3 指向源段序号。"""
    # 键用 (组控制号, 集控制号)，控制号在组间可能重用，不能只用集控制号。
    segment_ordinals: dict[tuple[str, str], dict] = {}
    for group, node in interchange.iter_sets():
        ordinals = {seg.start: i + 1 for i, seg in enumerate(node.segments)}
        segment_ordinals[(group.control, node.control)] = ordinals

    outcomes: dict[str, list[dict]] = {}
    for stored in stored_sets:
        problems = []
        # 从对应节点取回带偏移的错误，供 AK3 定位。
        node = _find_node(interchange, stored)
        if node is not None:
            ordinals = segment_ordinals[(stored.group_control, node.control)]
            for err in node.errors:
                problems.append({
                    "code": err.code,
                    "message": err.message,
                    "segment_tag": _segment_tag_at(node, err.offset),
                    "segment_ordinal": ordinals.get(err.offset),
                })
        for code in stored.reason_codes:
            if not any(p["code"] == code for p in problems):
                problems.append({"code": code, "segment_tag": None, "segment_ordinal": None})
        outcomes.setdefault(stored.group_control, []).append({
            "set_type": stored.set_type,
            "control": stored.control,
            "status": stored.status,
            "errors": problems,
        })
    return outcomes


def _find_node(interchange: Interchange, stored: StoredSet) -> SetNode | None:
    for group, node in interchange.iter_sets():
        if group.control == stored.group_control and node.control == stored.control:
            return node
    return None


def _segment_tag_at(node: SetNode, offset: int) -> str | None:
    for seg in node.segments:
        if seg.start == offset:
            return seg.tag
    return None


def _is_group_fatal(group) -> bool:
    return bool(group.errors)


def receive_raw(store: Store, raw: bytes) -> dict:
    """在已持锁的 Store 上接收一份 X12 原文，返回发送方可核对的收据。"""
    try:
        interchange = parse(raw)
    except InterchangeUnparseable as exc:
        return store.record_unparseable(raw, exc.message, exc.offset)

    stored_sets: list[StoredSet] = []
    group_fatal_controls = {g.control for g in interchange.groups if _is_group_fatal(g)}
    envelope_fatal = not interchange.ok

    for group, node in interchange.iter_sets():
        key = set_key(store.partner_id, interchange.control, group.control, node.control)
        warnings = ()
        correction_ref = ""
        supersedes = ""

        if envelope_fatal or group.control in group_fatal_controls:
            status = "REJECTED"
            reasons = tuple(["ENVELOPE_FATAL"] + [e.code for e in group.errors]
                            + [e.code for e in interchange.errors])
            activities: tuple = ()
        elif not node.ok:
            status = "REJECTED"
            reasons = tuple(e.code for e in node.errors)
            activities = ()
        else:
            content = extract_set(node)
            warnings = tuple(content.warnings)
            correction_ref = content.correction_ref
            if not content.ok:
                status = "REJECTED"
                reasons = tuple(e.code for e in content.errors)
                activities = ()
            else:
                supersedes, correction_problem = store.resolve_correction(interchange, content)
                if correction_problem:
                    status = "REJECTED"
                    reasons = (correction_problem,)
                    activities = ()
                else:
                    status = "ACCEPTED"
                    reasons = ()
                    annotated = []
                    for activity in content.activities:
                        row = activity.as_dict()
                        row["correction_of"] = supersedes
                        annotated.append(row)
                    activities = tuple(annotated)

        if not reasons and status == "REJECTED":
            reasons = ("UNSPECIFIED_REJECTION",)
        stored_sets.append(StoredSet(
            key=key,
            group_control=group.control,
            set_type=node.set_type,
            control=node.control,
            status=status,
            reason_codes=reasons,
            activities=activities,
            correction_ref=correction_ref,
            supersedes=supersedes,
            warnings=tuple(w.__dict__ for w in warnings),
            received_at="",
            interchange_sha256=interchange.raw_sha256,
        ))

    outcomes = _set_outcomes(interchange, stored_sets)
    acks = ack997.build_acknowledgments(interchange, outcomes)
    ack_payload = [
        {
            "group_control": group.control,
            "func_id": group.func_id,
            "encoding": "X12-997",
            "text": text,
            "sha256": hashlib.sha256(text.encode("latin-1")).hexdigest(),
        }
        for group, text in acks
    ]
    return store.record_ingest(interchange, stored_sets, ack_payload)
