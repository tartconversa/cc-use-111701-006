"""X12 997 功能确认生成。

每个入站功能组生成一份 997，AK2/AK5 逐交易集给出接受(A)/拒绝(R)，AK9 汇总
全部接受/全部拒绝/部分接受(P)。使用入站 ISA 声明的分隔符，保证与原件同一词法。

控制号确定性派生自原件哈希与组/集序号：同一字节内容重投，生成的 997 逐字节一致，
发送方可拿重投后的确认与首次确认直接核对。
"""
from __future__ import annotations

import hashlib

from .x12 import GroupNode, Interchange

_ACK_SET_TYPE = "997"
_ACK_FUNC_ID = "FA"
_ACK_VERSION = "004010"
# AK502/AK503 与 AK304 使用的标准错误码（004010 997）。
_CODE_SEGMENT_ERROR = "8"        # 段数据有误
_CODE_SEGMENT_UNEXPECTED = "2"   # 出现非预期段
_CODE_SET_ERROR = "5"            # 一个或多个数据段有误，交易集被拒
_CODE_COUNT_MISMATCH = "7"       # SE/GE 计数不符


def _control(seed: bytes, width: int = 9) -> str:
    """由原件哈希确定性派生数字控制号；全十进制空间取模，重投逐字节一致。"""
    value = int(hashlib.sha256(seed).hexdigest(), 16)
    return str(value % 10 ** width).zfill(width)


def _segment(tag: str, elements: list[str], element_sep: str, term: str) -> str:
    return tag + element_sep + element_sep.join(elements) + term


def _ak3_codes(error) -> str:
    code = error["code"] if isinstance(error, dict) else error.code
    if code in ("SE_COUNT_MISMATCH", "GE_COUNT_MISMATCH"):
        return _CODE_COUNT_MISMATCH
    if code in ("ST_WITHOUT_SE", "SE_WITHOUT_ST"):
        return _CODE_SEGMENT_UNEXPECTED
    return _CODE_SEGMENT_ERROR


def _build_one(interchange: Interchange, group: GroupNode,
               outcomes: list[dict], index: int) -> str:
    e_sep, c_sep, term = interchange.element_sep, interchange.component_sep, interchange.segment_term
    seed = interchange.raw_sha256.encode("ascii")
    isa_control = _control(seed)
    gs_control = _control(seed + b"|" + group.control.encode("latin-1"))

    lines: list[str] = []
    # 出站方向互换收发双方；ISA 固定宽度沿用入站已填充的字段。
    # 注意 ISA16（组件分隔符）不前置元素分隔符，其后直接是段终止符。
    isa_fields = [
        "00", " " * 10, "00", " " * 10,
        interchange.sender_qual, interchange.sender_id,
        interchange.receiver_qual, interchange.receiver_id,
        interchange.date, interchange.time, "U", "00401", isa_control, "0",
        "P",
    ]
    lines.append("ISA" + e_sep + e_sep.join(isa_fields) + e_sep + c_sep + term)
    gs_fields = [
        _ACK_FUNC_ID,
        interchange.sender_id.strip(), interchange.receiver_id.strip(),
        "20" + interchange.date, interchange.time, gs_control, "X", _ACK_VERSION,
    ]
    lines.append(_segment("GS", gs_fields, e_sep, term))
    lines.append(_segment("ST", [_ACK_SET_TYPE, str(index + 1).zfill(4)], e_sep, term))
    lines.append(_segment("AK1", [group.func_id, group.control], e_sep, term))

    accepted = rejected = 0
    for outcome in outcomes:
        lines.append(_segment("AK2", [outcome["set_type"], outcome["control"]], e_sep, term))
        # AK3 逐错误指向源交易集内的段序号与错误码。
        for problem in outcome["errors"]:
            ordinal = problem.get("segment_ordinal")
            if ordinal is not None:
                lines.append(_segment(
                    "AK3",
                    [problem.get("segment_tag", "?"), str(ordinal), "", _ak3_codes(problem)],
                    e_sep, term))
        if outcome["status"] == "ACCEPTED":
            accepted += 1
            lines.append(_segment("AK5", ["A"], e_sep, term))
        else:
            rejected += 1
            ak5 = ["R", _CODE_SET_ERROR]
            if any(p.get("code") in ("SE_COUNT_MISMATCH", "GE_COUNT_MISMATCH")
                   for p in outcome["errors"]):
                ak5.append(_CODE_COUNT_MISMATCH)
            lines.append(_segment("AK5", ak5, e_sep, term))

    total = accepted + rejected
    if rejected == 0:
        overall = "A"
    elif accepted == 0:
        overall = "R"
    else:
        overall = "P"
    lines.append(_segment("AK9", [overall, str(total), str(accepted), str(rejected)],
                          e_sep, term))

    # SE01 包含 ST 与 SE 自身。
    se_index = next(i for i, line in enumerate(lines) if line.startswith("ST" + e_sep))
    segment_count = len(lines) - se_index + 1
    lines.append(_segment("SE", [str(segment_count), str(index + 1).zfill(4)], e_sep, term))
    lines.append(_segment("GE", ["1", gs_control], e_sep, term))
    lines.append(_segment("IEA", ["1", isa_control], e_sep, term))
    return "".join(lines)


def build_acknowledgments(interchange: Interchange, outcomes_by_group: dict[str, list[dict]]):
    """返回 [(GroupNode, 997 原文)]；信封无任何功能组时返回空列表。"""
    result = []
    for index, group in enumerate(interchange.groups):
        outcomes = outcomes_by_group.get(group.control, [])
        result.append((group, _build_one(interchange, group, outcomes, index)))
    return result
