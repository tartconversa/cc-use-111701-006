"""852 产品活动数据（库存与动销）的业务内容提取。

只在信封解析出的单个交易集内部工作，失败只影响该交易集。提取出的每条 SKU 数量都带有
字节级来源位置（段起止偏移与元素下标），并保留业务地点（N1 ST/SH 等）。

修订单关联：头部 REF*CR（客户参考号，可配置限定符）指向原交易集的 ISA/GS/ST 控制号，
由存储层据此建立版本链；本模块只如实提取该引用，不做跨单据相加。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .x12 import ParseError, Segment, SetNode

SET_TYPE_852 = "852"
# 852 中常用的数量限定符；其余限定符原样保留，不臆造语义。
QTY_QUALIFIERS = {
    "02": "quantity_committed",
    "12": "quantity_on_order",
    "19": "quantity_on_hand",
    "31": "quantity_sold",
    "33": "quantity_available",
    "QA": "quantity_sold_ytd",
    "QOH": "quantity_on_hand",
}
# LIN 产品限定符中优先视为业务 SKU 的顺序。
SK_PREFERENCE = ("SK", "UP", "UA", "EN", "BP", "IT")


@dataclass(frozen=True)
class SourceLocation:
    segment_tag: str
    segment_start: int
    segment_end: int
    element_index: int  # 1 基；0 表示整段

    def as_dict(self) -> dict:
        return {
            "segment_tag": self.segment_tag,
            "segment_start": self.segment_start,
            "segment_end": self.segment_end,
            "element_index": self.element_index,
        }


@dataclass(frozen=True)
class Activity:
    sku: str
    sku_qualifier: str
    quantity_qualifier: str
    quantity_meaning: str
    quantity: str  # 原始数字文本，避免浮点
    unit: str
    business_location: str
    report_date: str
    correction_of: str  # 原交易集键，空串表示新单
    sources: tuple[SourceLocation, ...]

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "sku_qualifier": self.sku_qualifier,
            "quantity_qualifier": self.quantity_qualifier,
            "quantity_meaning": self.quantity_meaning,
            "quantity": self.quantity,
            "unit": self.unit,
            "business_location": self.business_location,
            "report_date": self.report_date,
            "correction_of": self.correction_of,
            "sources": [s.as_dict() for s in self.sources],
        }


@dataclass
class SetContent:
    set_type: str
    control: str
    activities: list[Activity] = field(default_factory=list)
    report_dates: list[str] = field(default_factory=list)
    correction_ref: str = ""
    errors: list[ParseError] = field(default_factory=list)
    warnings: list[ParseError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _pick_sku(seg_: Segment) -> tuple[str, str, SourceLocation] | None:
    """LIN 中成对出现 限定符/标识；按 SK_PREFERENCE 选业务 SKU，否则取第一对。"""
    candidates: list[tuple[int, str, str, int]] = []
    # LIN01 是行号，LIN02 起为 限定符, 标识, 限定符, 标识 ...
    i = 2
    while i + 1 < len(seg_.elements):
        qualifier, ident = seg_.elements[i], seg_.elements[i + 1]
        if qualifier and ident:
            try:
                rank = SK_PREFERENCE.index(qualifier)
            except ValueError:
                rank = len(SK_PREFERENCE)
            candidates.append((rank, qualifier, ident, i))
        i += 2
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, qualifier, ident, element_index = candidates[0]
    return ident, qualifier, SourceLocation("LIN", seg_.start, seg_.end, element_index)


def _parse_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def extract_set(node: SetNode, *, correction_qualifier: str = "CR") -> SetContent:
    """提取一个交易集；非 852 或内容错误时在 errors 中给出原因，不抛异常。"""
    content = SetContent(node.set_type, node.control)
    if node.set_type != SET_TYPE_852:
        content.errors.append(ParseError(
            "UNSUPPORTED_SET_TYPE", f"仅支持 852，收到 {node.set_type!r}", node.start,
            f"set:{node.control}"))
        return content
    # 信封层已经判定的错误原样保留，本单整体拒绝。
    content.errors.extend(node.errors)

    current_location = ""
    current_sku: tuple[str, str, SourceLocation] | None = None
    current_sources: list[SourceLocation] = []
    lin_seen = False

    for seg in node.segments:
        tag = seg.tag
        if tag in ("ST", "SE"):
            continue
        if tag == "XQ":
            # XQ01 报告类型、XQ02 报告日期。
            date = seg.el(2)
            if date:
                content.report_dates.append(date)
        elif tag == "N1":
            # N101 实体类型（ST 收货点 / SH 发货点 / Z7 仓库），N102 名称，N103/N104 代码标识。
            location_code = seg.el(4) or seg.el(2)
            if location_code:
                current_location = location_code
            # N1 开启新的报告位置循环：仅当已经在某个 LIN 循环内时才重置，
            # 正常的 N1 → LIN → QTY 顺序不受影响；重置后游离的 QTY 会被拒。
            if lin_seen:
                current_sku = None
                current_sources = []
        elif tag == "REF":
            qualifier = seg.el(1)
            value = seg.el(2)
            if qualifier == correction_qualifier and value:
                if lin_seen:
                    content.warnings.append(ParseError(
                        "REF_POSITION_SUSPECT", "修正引用 REF 出现在 LIN 之后，已按头部引用处理",
                        seg.start, f"set:{node.control}"))
                content.correction_ref = value
        elif tag == "LIN":
            lin_seen = True
            picked = _pick_sku(seg)
            if picked is None:
                content.errors.append(ParseError(
                    "LIN_NO_PRODUCT_ID", "LIN 缺少成对的产品限定符与标识", seg.start,
                    f"set:{node.control}"))
                current_sku = None
                continue
            current_sku = picked
            current_sources = [picked[2]]
        elif tag == "QTY":
            if current_sku is None:
                content.errors.append(ParseError(
                    "QTY_WITHOUT_LIN", "QTY 之前没有有效的 LIN", seg.start,
                    f"set:{node.control}"))
                continue
            qualifier = seg.el(1)
            raw_qty = seg.el(2)
            if not qualifier:
                content.errors.append(ParseError(
                    "QTY_MISSING_QUALIFIER", "QTY 缺少数量限定符", seg.start,
                    f"set:{node.control}"))
                continue
            number = _parse_decimal(raw_qty)
            if number is None or not raw_qty:
                content.errors.append(ParseError(
                    "QTY_NOT_NUMERIC", f"QTY 数量 {raw_qty!r} 不是数字", seg.start,
                    f"set:{node.control}"))
                continue
            if number < 0:
                content.errors.append(ParseError(
                    "QTY_NEGATIVE", f"QTY 数量 {raw_qty} 为负", seg.start,
                    f"set:{node.control}"))
                continue
            sku, sku_qualifier, sku_source = current_sku
            qty_source = SourceLocation("QTY", seg.start, seg.end, 2)
            report_date = content.report_dates[-1] if content.report_dates else ""
            content.activities.append(Activity(
                sku=sku,
                sku_qualifier=sku_qualifier,
                quantity_qualifier=qualifier,
                quantity_meaning=QTY_QUALIFIERS.get(qualifier, "quantity_other"),
                quantity=raw_qty,
                unit=seg.el(3),
                business_location=current_location,
                report_date=report_date,
                correction_of="",
                sources=tuple(current_sources + [qty_source]),
            ))
        # 其余 852 段（CTP、SDQ、ZA、ZQ 等）按契约范围忽略，不报错。

    if not content.errors and not content.activities:
        content.errors.append(ParseError(
            "NO_ACTIVITY", "交易集内没有任何有效的 SKU 数量条目", node.start,
            f"set:{node.control}"))
    return content
