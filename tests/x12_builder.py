"""合成 X12 交换的测试夹具构建器（合成数据，不接触真实企业系统）。

刻意支持非典型分隔符，验证解析器不假设固定字符。segments 中以 ST:/SE:/GS:/GE:/IEA:
开头的字符串为裸段，由测试自行构造（用于注入计数错误等），构建器不再自动包装。
"""
from __future__ import annotations

ISA_TEMPLATE = (
    "ISA"
    "{e}00"                       # ISA01 2
    "{e}          "               # ISA02 10
    "{e}00"                       # ISA03 2
    "{e}          "               # ISA04 10
    "{e}{sender_qual}"            # ISA05 2
    "{e}{sender_id:<15.15}"       # ISA06 15
    "{e}{receiver_qual}"          # ISA07 2
    "{e}{receiver_id:<15.15}"     # ISA08 15
    "{e}{date}"                   # ISA09 6
    "{e}{time}"                   # ISA10 4
    "{e}U"                        # ISA11 1
    "{e}00401"                    # ISA12 5
    "{e}{isa_control:>9.9}"       # ISA13 9
    "{e}0"                        # ISA14 1
    "{e}P"                        # ISA15 1
    "{e}{component}"              # ISA16 1（组件分隔符作为元素值）
)


def seg(tag: str, *elements: str, element="*", term="~") -> str:
    return element.join((tag,) + elements) + term


def _rewrite(raw: str, element: str, term: str) -> str:
    """把用占位符（*、~）书写的段转成交换声明的分隔符。"""
    if element == "*" and term == "~":
        return raw
    return raw.replace("*", element).replace("~", term)


def build_interchange(*, isa_control="000000001", groups, element="*", component=":",
                      term="~", sender_qual="ZZ", sender_id="DEALER-A",
                      receiver_qual="ZZ", receiver_id="RECEIVER",
                      date="261118", time="1200", extra_tail="") -> bytes:
    """groups: [{"control": "1", "sets": [(st_control, [业务段... 或裸 ST/SE 段])]}]"""
    head = ISA_TEMPLATE.format(
        e=element, component=component, sender_qual=sender_qual, sender_id=sender_id,
        receiver_qual=receiver_qual, receiver_id=receiver_id, date=date, time=time,
        isa_control=isa_control,
    )
    parts = [head + term]
    for group in groups:
        gs_raw = group.get("gs_raw")
        if gs_raw is not None:
            parts.append(gs_raw)
        else:
            parts.append(element.join([
                "GS", group.get("func_id", "PR"), sender_id, receiver_id, date, time,
                group["control"], "X", "004010",
            ]) + term)
        for entry in group["sets"]:
            st_control, segments = entry
            # 业务段用占位分隔符（* 与 ~）书写，这里统一转成本交换声明的分隔符。
            normalized = [_rewrite(s, element, term) for s in segments]
            has_explicit_st = any(s.startswith("ST" + element) for s in normalized)
            has_explicit_se = any(s.startswith("SE" + element) for s in normalized)
            business = [s for s in normalized
                        if not (s.startswith("ST" + element) or s.startswith("SE" + element))]
            if not has_explicit_st:
                parts.append(element.join(["ST", group.get("set_type", "852"), st_control]) + term)
            for raw in normalized:
                parts.append(raw if raw.endswith(term) else raw + term)
            if not has_explicit_se:
                count = 1 + len(business) + 1  # ST + 业务段 + SE
                parts.append(element.join(["SE", str(count), st_control]) + term)
        ge_raw = group.get("ge_raw")
        if ge_raw is not None:
            parts.append(_rewrite(ge_raw, element, term))
        else:
            parts.append(element.join(["GE", str(len(group["sets"])), group["control"]]) + term)
    iea_raw = groups[0].get("iea_raw") if groups else None
    if iea_raw is not None:
        parts.append(_rewrite(iea_raw, element, term))
    else:
        parts.append(element.join(["IEA", str(len(groups)), isa_control]) + term)
    text = "".join(parts) + extra_tail
    return text.encode("latin-1")


def good_852_segments(*, location="WH-01", sku="G20", qty_qualifier="19", qty="12",
                      report_date="2026-11-18", extra_skus=(), element="*", term="~"):
    lines = [
        seg("XQ", "DR", report_date, element=element, term=term),
        seg("N1", "ST", "STORE-ONE", "92", location, element=element, term=term),
        seg("LIN", "001", "UP", "00012345600012", "SK", sku, element=element, term=term),
        seg("QTY", qty_qualifier, qty, "EA", element=element, term=term),
    ]
    for sku_id, q in extra_skus:
        lines.append(seg("LIN", "002", "SK", sku_id, element=element, term=term))
        lines.append(seg("QTY", "31", q, "EA", element=element, term=term))
    return lines


def correction_852_segments(original_ref: str, *, sku="G20", qty="40",
                            element="*", term="~"):
    return [
        seg("XQ", "DR", "2026-11-19", element=element, term=term),
        seg("REF", "CR", original_ref, element=element, term=term),
        seg("N1", "ST", "STORE-ONE", "92", "WH-01", element=element, term=term),
        seg("LIN", "001", "SK", sku, element=element, term=term),
        seg("QTY", "19", qty, "EA", element=element, term=term),
    ]
