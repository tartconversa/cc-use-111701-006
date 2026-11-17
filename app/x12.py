"""X12 004010 交换的词法与信封解析。

分隔符完全由 ISA 段决定（元素分隔符在 ISA 内第 4 个字符、组件分隔符在第 105 个、
段终止符在 106 个固定字符之后），不假设任何固定字符。

信封按 ISA/GS/ST 的控制号与 IEA/GE/SE 的回指控制号、数量计数核对。错误分三层收集：
交换级（整份交换不可用）、功能组级（本组交易集全部受牵连）、交易集级（仅该单据失败，
同组其他单据的边界仍然保留）。每个段记录在原始字节中的起止偏移，供来源定位。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# ISA 段固定为 106 个字符（含段终止符）：索引 3 为元素分隔符，索引 104 为组件分隔符，
# 索引 105 为段终止符。这是 X12 ISA 的固定词法，分隔符本身由发送方在这三个位置声明。
ISA_FIXED_LENGTH = 106
# ISA01..ISA16 的固定字段宽度。
ISA_FIELD_WIDTHS = (2, 10, 2, 10, 2, 15, 2, 15, 6, 4, 1, 5, 9, 1, 1, 1)
SUPPORTED_CONTROL_VERSION = "00401"


class InterchangeUnparseable(Exception):
    """ISA 无法识别或信封根结构损坏，连分隔符都无法确定时抛出。"""

    def __init__(self, message: str, raw_sha256: str, offset: int = -1):
        super().__init__(message)
        self.message = message
        self.raw_sha256 = raw_sha256
        self.offset = offset


@dataclass(frozen=True)
class Segment:
    tag: str
    elements: tuple[str, ...]
    start: int  # 段在原文中的起始字节偏移
    end: int  # 段结束偏移（含终止符，不含则为开区间）

    def el(self, index: int) -> str:
        """1 基元素下标（如 ST02 写作 el(2)）；缺失返回空串。"""
        return self.elements[index] if 0 <= index < len(self.elements) else ""


@dataclass(frozen=True)
class ParseError:
    code: str
    message: str
    offset: int = -1
    scope: str = ""


@dataclass(frozen=True)
class SetNode:
    set_type: str
    control: str
    segments: tuple[Segment, ...]
    start: int
    end: int
    errors: tuple[ParseError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class GroupNode:
    func_id: str
    control: str
    version: str
    sets: tuple[SetNode, ...]
    start: int
    end: int
    errors: tuple[ParseError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class Interchange:
    raw: bytes
    element_sep: str
    component_sep: str
    segment_term: str
    sender_qual: str
    sender_id: str
    receiver_qual: str
    receiver_id: str
    control: str
    date: str
    time: str
    groups: tuple[GroupNode, ...]
    errors: tuple[ParseError, ...]
    raw_sha256: str

    @property
    def ok(self) -> bool:
        return not self.errors

    def iter_sets(self):
        for group in self.groups:
            for node in group.sets:
                yield group, node


def _tokenize(text: str, element_sep: str, term: str) -> tuple[list[Segment], list[ParseError]]:
    segments: list[Segment] = []
    errors: list[ParseError] = []
    start = 0
    for i, ch in enumerate(text):
        if ch != term:
            continue
        piece = text[start:i]
        if piece.endswith("\r"):
            piece = piece[:-1]
        stripped = piece.strip()
        if stripped:
            elements = tuple(stripped.split(element_sep))
            segments.append(Segment(elements[0], elements, start, i + 1))
        start = i + 1
    tail = text[start:].strip()
    if tail:
        errors.append(ParseError("TRAILING_CONTENT", "最后一个段终止符之后存在游离内容", start))
    return segments, errors


def parse(raw: bytes) -> Interchange:
    """解析一整份交换；信封级致命问题抛 InterchangeUnparseable，其余错误挂在节点上。"""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("X12 原文必须是字节")
    data = bytes(raw)
    digest = hashlib.sha256(data).hexdigest()
    text = data.decode("latin-1")  # 一字节一码元，偏移与字节位置一致

    if len(text) < ISA_FIXED_LENGTH + 1 or not text.startswith("ISA"):
        raise InterchangeUnparseable("缺少 ISA 起始段，无法确定分隔符", digest)
    element_sep = text[3]
    component_sep = text[104]
    term = text[105]
    if not (element_sep.isprintable() and component_sep.isprintable() and term.isprintable()):
        raise InterchangeUnparseable("ISA 给出的分隔符包含不可打印字符", digest, 105)
    if len({element_sep, component_sep, term}) != 3:
        raise InterchangeUnparseable("元素分隔符、组件分隔符与段终止符必须互不相同", digest, 3)

    # 核心在段终止符前结束；按元素分隔符拆分应得到 17 段（ISA + 16 元素）。
    isa_core = text[:ISA_FIXED_LENGTH - 1]
    isa_parts = isa_core.split(element_sep)
    if len(isa_parts) != 17:
        raise InterchangeUnparseable(
            f"ISA 应含 16 个元素，实际拆分出 {len(isa_parts) - 1} 个", digest, 0
        )
    position = 3
    for width, value in zip(ISA_FIELD_WIDTHS, isa_parts[1:]):
        if len(value) != width:
            raise InterchangeUnparseable(
                f"ISA 固定宽度元素长度错误：期望 {width}，实际 {len(value)}（{value!r}）",
                digest, position,
            )
        position += width + 1
    if isa_parts[16] != component_sep:
        raise InterchangeUnparseable("ISA16 必须等于声明的组件分隔符", digest, 104)
    if isa_parts[12] != SUPPORTED_CONTROL_VERSION:
        raise InterchangeUnparseable(
            f"仅支持控制版本 00401，ISA12={isa_parts[12]!r}", digest, 0
        )

    segments, errors = _tokenize(text, element_sep, term)
    if not segments or segments[0].tag != "ISA":
        raise InterchangeUnparseable("首个段必须是 ISA", digest, 0)

    isa = segments[0]
    groups: list[GroupNode] = []
    group_sets: list[SetNode] = []
    group_errors: list[ParseError] = []
    group_start = -1
    group_control = ""
    func_id = ""
    version = ""

    set_segments: list[Segment] = []
    set_errors: list[ParseError] = []
    set_control = ""
    set_type = ""
    set_start = -1
    in_set = False
    seen_set_controls: set[str] = set()

    def close_set(end_offset: int) -> None:
        nonlocal set_segments, set_errors, set_control, set_type, set_start, in_set
        if not in_set:
            return
        node = SetNode(
            set_type or "", set_control, tuple(set_segments), set_start, end_offset,
            tuple(set_errors),
        )
        group_sets.append(node)
        set_segments, set_errors = [], []
        set_control, set_type, set_start, in_set = "", "", -1, False

    def close_group(end_offset: int) -> None:
        nonlocal group_sets, group_errors, group_start, group_control, func_id, version
        nonlocal seen_set_controls
        close_set(end_offset)
        groups.append(
            GroupNode(
                func_id, group_control, version, tuple(group_sets), group_start, end_offset,
                tuple(group_errors),
            )
        )
        group_sets, group_errors = [], []
        group_start, group_control, func_id, version = -1, "", "", ""
        seen_set_controls = set()

    for seg in segments[1:]:
        tag = seg.tag
        if tag == "ISA":
            raise InterchangeUnparseable("一次传输只允许一份交换，检测到第二个 ISA", digest, seg.start)
        if tag == "GS":
            if group_start >= 0:
                group_errors.append(
                    ParseError("GS_WITHOUT_GE", "新 GS 出现时上一功能组尚未 GE 关闭", seg.start,
                               f"group:{group_control}")
                )
                close_group(seg.start)
            group_start = seg.start
            func_id = seg.el(1)
            group_control = seg.el(6)
            version = seg.el(8)
        elif tag == "ST":
            if group_start < 0:
                errors.append(ParseError("ST_WITHOUT_GS", "ST 出现在功能组之外", seg.start))
                continue
            if in_set:
                set_errors.append(
                    ParseError("ST_WITHOUT_SE", "新 ST 出现时上一交易集尚未 SE 关闭", seg.start,
                               f"set:{set_control}")
                )
                close_set(seg.start)
            set_type = seg.el(1)
            set_control = seg.el(2)
            if not set_type or not set_control:
                set_errors.append(ParseError("ST_INCOMPLETE", "ST 缺少交易集类型或控制号",
                                             seg.start, "set:"))
            elif set_control in seen_set_controls:
                set_errors.append(ParseError("DUPLICATE_ST_CONTROL",
                                             f"组内 ST 控制号 {set_control} 重复", seg.start,
                                             f"set:{set_control}"))
            else:
                seen_set_controls.add(set_control)
            set_start = seg.start
            set_segments = [seg]
            in_set = True
        elif tag == "SE":
            if not in_set:
                group_errors.append(ParseError("SE_WITHOUT_ST", "SE 没有对应的 ST", seg.start,
                                               f"group:{group_control}"))
                continue
            set_segments.append(seg)
            if seg.el(2) != set_control:
                set_errors.append(ParseError(
                    "SE_CONTROL_MISMATCH",
                    f"SE 控制号 {seg.el(2)!r} 与 ST 控制号 {set_control!r} 不一致",
                    seg.start, f"set:{set_control}"))
            expected_count = len(set_segments)
            try:
                declared = int(seg.el(1))
            except ValueError:
                declared = -1
            if declared != expected_count:
                set_errors.append(ParseError(
                    "SE_COUNT_MISMATCH",
                    f"SE 声明 {declared} 段，实际 ST..SE 共 {expected_count} 段",
                    seg.start, f"set:{set_control}"))
            close_set(seg.end)
        elif tag == "GE":
            if group_start < 0:
                errors.append(ParseError("GE_WITHOUT_GS", "GE 没有对应的 GS", seg.start))
                continue
            if seg.el(2) != group_control:
                group_errors.append(ParseError(
                    "GE_CONTROL_MISMATCH",
                    f"GE 控制号 {seg.el(2)!r} 与 GS 控制号 {group_control!r} 不一致",
                    seg.start, f"group:{group_control}"))
            try:
                declared_sets = int(seg.el(1))
            except ValueError:
                declared_sets = -1
            if declared_sets != len(group_sets):
                group_errors.append(ParseError(
                    "GE_COUNT_MISMATCH",
                    f"GE 声明 {declared_sets} 个交易集，实际 {len(group_sets)} 个",
                    seg.start, f"group:{group_control}"))
            close_group(seg.end)
        elif tag == "IEA":
            if group_start >= 0:
                group_errors.append(ParseError("MISSING_GE", "IEA 出现时功能组尚未 GE 关闭",
                                               seg.start, f"group:{group_control}"))
                close_group(seg.start)
            if seg.el(2) != isa.el(13):
                errors.append(ParseError(
                    "IEA_CONTROL_MISMATCH",
                    f"IEA 控制号 {seg.el(2)!r} 与 ISA 控制号 {isa.el(13)!r} 不一致",
                    seg.start))
            try:
                declared_groups = int(seg.el(1))
            except ValueError:
                declared_groups = -1
            if declared_groups != len(groups):
                errors.append(ParseError(
                    "IEA_COUNT_MISMATCH",
                    f"IEA 声明 {declared_groups} 个功能组，实际 {len(groups)} 个", seg.start))
        else:
            # 业务段：交易集内收集，集外游离属信封错误。
            if in_set:
                set_segments.append(seg)
            elif group_start >= 0:
                group_errors.append(ParseError("SEGMENT_OUTSIDE_SET",
                                               f"业务段 {tag} 出现在交易集之外", seg.start,
                                               f"group:{group_control}"))
            else:
                errors.append(ParseError("SEGMENT_OUTSIDE_ENVELOPE",
                                         f"段 {tag} 出现在任何功能组之外", seg.start))

    if group_start >= 0 or in_set:
        errors.append(ParseError("MISSING_IEA", "原文结束但缺少 GE/IEA 关闭信封", len(text)))

    return Interchange(
        raw=data,
        element_sep=element_sep,
        component_sep=component_sep,
        segment_term=term,
        sender_qual=isa.el(5),
        sender_id=isa.el(6),
        receiver_qual=isa.el(7),
        receiver_id=isa.el(8),
        control=isa.el(13),
        date=isa.el(9),
        time=isa.el(10),
        groups=tuple(groups),
        errors=tuple(errors),
        raw_sha256=digest,
    )
