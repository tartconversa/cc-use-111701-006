import tempfile
import unittest
from pathlib import Path

from app import x12
from app.activity import extract_set
from app.edi import receive_raw
from app.store import Store, parse_correction_ref, set_key
from app.x12 import InterchangeUnparseable, parse

from tests.x12_builder import (
    build_interchange, correction_852_segments, good_852_segments, seg,
)

PARTNER = "dealer-a"


class ParserTest(unittest.TestCase):
    def test_delimiters_come_from_isa_not_assumed(self):
        # 元素 |、组件 ^、段终止符 #，并带尾随换行（真实传输常见）。
        raw = build_interchange(
            groups=[{"control": "G1", "sets": [("S1", good_852_segments())]}],
            element="|", component="^", term="#", extra_tail="\r\n",
        )
        interchange = parse(raw)
        self.assertEqual(interchange.element_sep, "|")
        self.assertEqual(interchange.component_sep, "^")
        self.assertEqual(interchange.segment_term, "#")
        self.assertEqual(interchange.control, "000000001")
        self.assertEqual(len(interchange.groups), 1)
        group = interchange.groups[0]
        self.assertEqual(group.control, "G1")
        self.assertEqual(len(group.sets), 1)
        node = group.sets[0]
        self.assertEqual(node.set_type, "852")
        self.assertEqual(node.control, "S1")
        self.assertTrue(node.ok)

    def test_segments_carry_byte_offsets(self):
        raw = build_interchange(
            groups=[{"control": "G1", "sets": [("S1", good_852_segments())]}],
        )
        node = parse(raw).groups[0].sets[0]
        lin = next(s for s in node.segments if s.tag == "LIN")
        qty = next(s for s in node.segments if s.tag == "QTY")
        self.assertEqual(raw[lin.start:lin.start + 3], b"LIN")
        self.assertEqual(raw[qty.start:qty.start + 3], b"QTY")
        self.assertGreater(qty.start, lin.start)
        # 偏移是字节位置，可直接索引原文。
        self.assertIn(b"QTY*19*12*EA~", raw[qty.start:qty.end])

    def test_bad_isa_is_unparseable_without_touching_assumptions(self):
        with self.assertRaises(InterchangeUnparseable):
            parse(b"NOTANISA")
        # 分隔符冲突也要拒绝。
        with self.assertRaises(InterchangeUnparseable):
            build_and_parse_isa_with_clash()

    def test_duplicate_delimiters_rejected(self):
        raw = build_interchange(
            groups=[{"control": "G1", "sets": [("S1", good_852_segments())]}],
            element="~", component=":", term="~",
        )
        with self.assertRaises(InterchangeUnparseable):
            parse(raw)

    def test_envelope_control_and_count_checks(self):
        # SE 控制号不匹配 → 仅该交易集错误。
        raw = build_interchange(groups=[{
            "control": "G1",
            "sets": [
                ("S1", good_852_segments()),
                ("S2", good_852_segments(sku="G21")),
            ],
        }])
        broken = raw.replace(b"SE*6*S2~", b"SE*6*S9~", 1)
        self.assertNotEqual(broken, raw)
        interchange = parse(broken)
        s1, s2 = interchange.groups[0].sets
        self.assertTrue(s1.ok)
        self.assertFalse(s2.ok)
        self.assertIn("SE_CONTROL_MISMATCH", [e.code for e in s2.errors])

    def test_group_count_mismatch_marks_group(self):
        raw = build_interchange(groups=[{
            "control": "G1",
            "ge_raw": "GE*9*G1~",
            "sets": [("S1", good_852_segments())],
        }])
        interchange = parse(raw)
        self.assertIn("GE_COUNT_MISMATCH",
                      [e.code for e in interchange.groups[0].errors])

    def test_iea_control_mismatch_is_interchange_level(self):
        raw = build_interchange(groups=[{
            "control": "G1",
            "iea_raw": "IEA*1*999999999~",
            "sets": [("S1", good_852_segments())],
        }])
        interchange = parse(raw)
        self.assertIn("IEA_CONTROL_MISMATCH", [e.code for e in interchange.errors])


def build_and_parse_isa():
    return parse(build_interchange(groups=[
        {"control": "G1", "sets": [("S1", good_852_segments())]}]))


def build_and_parse_isa_with_clash():
    # 元素分隔符与组件分隔符相同，ISA 校验应拒绝。
    raw = build_interchange(
        groups=[{"control": "G1", "sets": [("S1", good_852_segments())]}],
        element="*", component="*", term="~",
    )
    return parse(raw)


class ActivityTest(unittest.TestCase):
    def test_sk_qualifier_preferred_and_quantities_located(self):
        node = build_and_parse_isa().groups[0].sets[0]
        content = extract_set(node)
        self.assertTrue(content.ok, [e.message for e in content.errors])
        activity = content.activities[0]
        self.assertEqual(activity.sku, "G20")          # LIN 同时有 UP/SK，取 SK
        self.assertEqual(activity.sku_qualifier, "SK")
        self.assertEqual(activity.quantity, "12")
        self.assertEqual(activity.quantity_qualifier, "19")
        self.assertEqual(activity.business_location, "WH-01")
        tags = [s.segment_tag for s in activity.sources]
        self.assertEqual(tags, ["LIN", "QTY"])
        self.assertTrue(all(s.segment_start >= 0 for s in activity.sources))

    def test_qty_without_lin_rejects_only_that_set(self):
        # S2 内第二个 QTY 没有新 LIN 承接（夹在 N1 之后），仅该交易集失败。
        bad_segments = [
            seg("XQ", "DR", "2026-11-18"),
            seg("N1", "ST", "STORE-TWO", "92", "WH-02"),
            seg("LIN", "001", "SK", "G21"),
            seg("QTY", "19", "8", "EA"),
            seg("N1", "ST", "STORE-THREE", "92", "WH-03"),
            seg("QTY", "19", "7", "EA"),
        ]
        raw = build_interchange(groups=[{"control": "G1", "sets": [
            ("S1", good_852_segments()),
            ("S2", bad_segments),
        ]}])
        interchange = parse(raw)
        good_node, bad_node = interchange.groups[0].sets
        self.assertTrue(extract_set(good_node).ok)
        bad_content = extract_set(bad_node)
        self.assertFalse(bad_content.ok)
        self.assertIn("QTY_WITHOUT_LIN", [e.code for e in bad_content.errors])

    def test_non_numeric_quantity_rejected(self):
        segments = [
            seg("LIN", "001", "SK", "G30"),
            seg("QTY", "19", "abc", "EA"),
        ]
        raw = build_interchange(groups=[{"control": "G1", "sets": [("S1", segments)]}])
        content = extract_set(parse(raw).groups[0].sets[0])
        self.assertFalse(content.ok)
        self.assertIn("QTY_NOT_NUMERIC", [e.code for e in content.errors])

    def test_set_without_any_activity_rejected(self):
        raw = build_interchange(groups=[{"control": "G1", "sets": [
            ("S1", [seg("N1", "ST", "EMPTY-STORE", "92", "WH-9")]),
        ]}])
        content = extract_set(parse(raw).groups[0].sets[0])
        self.assertIn("NO_ACTIVITY", [e.code for e in content.errors])


def _receive(raw: bytes, root: str, partner: str = PARTNER) -> dict:
    with Store(root, partner).locked() as store:
        return receive_raw(store, raw)


class ReceiptAndAcknowledgmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _two_sets_one_broken(self):
        raw = build_interchange(groups=[{"control": "G1", "sets": [
            ("S1", good_852_segments()),
            ("S2", good_852_segments(sku="G21")),
        ]}])
        return raw.replace(b"SE*6*S2~", b"SE*9*S2~", 1)

    def test_partial_failure_keeps_good_documents_accepted(self):
        receipt = _receive(self._two_sets_one_broken(), self.root)
        statuses = {s["control"]: s["status"] for s in receipt["sets"]}
        self.assertEqual(statuses["S1"], "ACCEPTED")
        self.assertEqual(statuses["S2"], "REJECTED")
        self.assertIn("SE_COUNT_MISMATCH", receipt["sets"][1]["reason_codes"])
        ack = receipt["acknowledgments"][0]["text"]
        self.assertIn("AK2*852*S1~", ack)
        self.assertIn("AK5*A~", ack)
        self.assertIn("AK2*852*S2~", ack)
        self.assertIn("AK5*R", ack)
        self.assertIn("AK9*P*2*1*1~", ack)  # 部分接受

    def test_997_uses_inbound_delimiters_and_is_deterministic(self):
        raw = build_interchange(
            groups=[{"control": "G1", "sets": [("S1", good_852_segments())]}],
            element="|", component="^", term="#",
        )
        first = _receive(raw, self.root)
        ack_first = first["acknowledgments"][0]["text"]
        self.assertTrue(ack_first.startswith("ISA|"))
        self.assertIn("|", ack_first)
        self.assertNotIn("*", ack_first)
        self.assertTrue(ack_first.endswith("#"))
        second = _receive(raw, self.root)  # 断线重投
        self.assertEqual(second["acknowledgments"][0]["text"], ack_first)
        self.assertTrue(second["replay"])

    def test_identical_redelivery_is_idempotent_after_restart(self):
        raw = build_interchange(groups=[{"control": "G1", "sets": [
            ("S1", good_852_segments())]}])
        first = _receive(raw, self.root)
        # 模拟进程重启：重新打开同一目录，从仅追加日志回放。
        second = _receive(raw, self.root)
        self.assertEqual(second["receipt_id"], first["receipt_id"])
        self.assertTrue(second["replay"])
        log_lines = (Path(self.root) / "events.log").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(log_lines), 1, "重投不得追加第二份接收结论")

    def test_same_control_different_bytes_conflicts_and_never_overwrites(self):
        raw = build_interchange(
            isa_control="000000077",
            groups=[{"control": "G1", "sets": [("S1", good_852_segments())]}],
        )
        first = _receive(raw, self.root)
        self.assertEqual(first["disposition"], "PROCESSED")
        changed = build_interchange(
            isa_control="000000077",
            groups=[{"control": "G1", "sets": [
                ("S1", good_852_segments(qty="99"))]}],
        )
        conflict = _receive(changed, self.root)
        self.assertEqual(conflict["disposition"], "REJECTED_INTERCHANGE")
        self.assertEqual(conflict["reason"], "DUPLICATE_CONTROL_DIFFERENT_BYTES")
        # 原结论保持不变。
        again = _receive(raw, self.root)
        self.assertTrue(again["replay"])
        self.assertEqual(again["sets"][0]["activity_count"], 1)

    def test_control_numbers_reused_across_partners_are_independent(self):
        raw = build_interchange(
            isa_control="000000050",
            groups=[{"control": "G1", "sets": [("S1", good_852_segments())]}],
        )
        a = _receive(raw, self.root, partner="dealer-a")
        b = _receive(raw, self.root, partner="dealer-b")
        self.assertEqual(a["disposition"], "PROCESSED")
        self.assertEqual(b["disposition"], "PROCESSED")
        self.assertFalse(b["replay"])

    def test_correction_supersedes_instead_of_summing(self):
        original = build_interchange(
            isa_control="000000010",
            groups=[{"control": "G1", "sets": [("S1", good_852_segments(qty="12"))]}],
        )
        r1 = _receive(original, self.root)
        original_key = r1["sets"][0]["set_key"]
        # REF*CR 指向原 ISA/GS/ST 控制号。
        ref = "000000010/G1/S1"
        corrected = build_interchange(
            isa_control="000000011",
            groups=[{"control": "G2", "sets": [
                ("S2", correction_852_segments(ref, qty="40"))]}],
        )
        r2 = _receive(corrected, self.root)
        self.assertEqual(r2["sets"][0]["status"], "ACCEPTED")
        self.assertEqual(r2["sets"][0]["supersedes"], original_key)
        with Store(self.root, PARTNER).locked() as store:
            view = store.current_quantities()
            lineage = store.lineage(original_key)
        # 当前视图只有链头的 40，不是 12+40=52。
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["quantity"], "40")
        self.assertEqual(lineage, [original_key, r2["sets"][0]["set_key"]])

    def test_correction_pointing_at_unknown_original_is_rejected(self):
        ref = "000000999/GX/SS"
        corrected = build_interchange(
            isa_control="000000012",
            groups=[{"control": "G2", "sets": [
                ("S3", correction_852_segments(ref))]}],
        )
        result = _receive(corrected, self.root)
        self.assertEqual(result["sets"][0]["status"], "REJECTED")
        self.assertIn("ORIGINAL_NOT_FOUND", result["sets"][0]["reason_codes"])

    def test_unparseable_transport_still_returns_checkable_receipt(self):
        result = _receive(b"<<< not x12 >>>", self.root)
        self.assertEqual(result["disposition"], "REJECTED_INTERCHANGE")
        self.assertEqual(result["reason"], "INTERCHANGE_UNPARSEABLE")
        self.assertEqual(len(result["raw_sha256"]), 64)
        # 同字节重投，结论保持一致。
        again = _receive(b"<<< not x12 >>>", self.root)
        self.assertEqual(again["raw_sha256"], result["raw_sha256"])
        self.assertTrue(again["replay"])

    def test_group_fatal_rejects_group_but_not_other_groups(self):
        raw = build_interchange(
            isa_control="000000020",
            groups=[
                {"control": "G1", "ge_raw": "GE*9*G1~",
                 "sets": [("S1", good_852_segments())]},
                {"control": "G2", "sets": [("S2", good_852_segments(sku="G22"))]},
            ],
        )
        result = _receive(raw, self.root)
        by_group = {s["group_control"]: s["status"] for s in result["sets"]}
        self.assertEqual(by_group["G1"], "REJECTED")
        self.assertEqual(by_group["G2"], "ACCEPTED")
        # 每个功能组各一份 997。
        self.assertEqual(len(result["acknowledgments"]), 2)

    def test_receipt_includes_provenance_for_every_quantity(self):
        raw = build_interchange(groups=[{"control": "G1", "sets": [
            ("S1", good_852_segments(extra_skus=(("G99", "5"),)))]}])
        result = _receive(raw, self.root)
        with Store(self.root, PARTNER).locked() as store:
            rows = store.current_quantities()
        self.assertEqual({r["sku"] for r in rows}, {"G20", "G99"})
        for row in rows:
            self.assertTrue(row["sources"])
            self.assertTrue(all("segment_start" in s for s in row["sources"]))
            self.assertEqual(row["business_location"], "WH-01")

    def test_correction_ref_parsing_helpers(self):
        key = parse_correction_ref(PARTNER, "ISA1", "000000010/G1/S1")
        self.assertEqual(key, set_key(PARTNER, "000000010", "G1", "S1"))
        self.assertEqual(parse_correction_ref(PARTNER, "ISA1", "bad-ref"), "")

    def test_warning_path_is_persisted_without_crashing(self):
        # REF*CR 出现在 LIN 之后会产生告警；验证告警能随收据持久化（回归 __dict__ 缺陷）。
        original = build_interchange(
            isa_control="000000040",
            groups=[{"control": "G1", "sets": [("S1", good_852_segments())]}],
        )
        _receive(original, self.root)
        late_ref = [
            seg("XQ", "DR", "2026-11-20"),
            seg("N1", "ST", "STORE-ONE", "92", "WH-01"),
            seg("LIN", "001", "SK", "G20"),
            seg("QTY", "19", "9", "EA"),
            seg("REF", "CR", "000000040/G1/S1"),
        ]
        corrected = build_interchange(
            isa_control="000000041",
            groups=[{"control": "G2", "sets": [("S2", late_ref)]}],
        )
        result = _receive(corrected, self.root)
        target = next(s for s in result["sets"] if s["control"] == "S2")
        self.assertEqual(target["status"], "ACCEPTED")
        self.assertTrue(target["warnings"])
        self.assertEqual(target["warnings"][0]["code"], "REF_POSITION_SUSPECT")
        # 重放后告警仍在，且重投不重复写日志。
        again = _receive(corrected, self.root)
        self.assertTrue(again["replay"])
        self.assertTrue(again["sets"][0]["warnings"])

    def test_set_control_reused_across_groups_stays_distinct(self):
        # 两个组都用 ST 控制号 "S1"，必须作为两个独立交易集处理，AK3 序号不串。
        raw = build_interchange(
            isa_control="000000030",
            groups=[
                {"control": "G1", "sets": [("S1", good_852_segments(sku="G20"))]},
                {"control": "G2", "sets": [("S1", good_852_segments(sku="G21"))]},
            ],
        )
        result = _receive(raw, self.root)
        keys = [s["set_key"] for s in result["sets"]]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(s["status"] == "ACCEPTED" for s in result["sets"]))
        self.assertEqual(len(result["acknowledgments"]), 2)
        for ack in result["acknowledgments"]:
            self.assertIn("AK2*852*S1~", ack["text"])

    def test_duplicate_st_control_within_group_rejected_individually(self):
        # 同组内两个 ST 用相同控制号：第二个拒绝，第一个仍接受。
        raw = build_interchange(
            isa_control="000000031",
            groups=[{"control": "G1", "sets": [
                ("DUP", good_852_segments(sku="G20")),
                ("DUP", good_852_segments(sku="G21")),
            ]}],
        )
        result = _receive(raw, self.root)
        statuses = [s["status"] for s in result["sets"]]
        self.assertEqual(statuses, ["ACCEPTED", "REJECTED"])
        self.assertIn("DUPLICATE_ST_CONTROL", result["sets"][1]["reason_codes"])

    def test_original_bytes_are_preserved_content_addressed(self):
        raw = build_interchange(groups=[{"control": "G1", "sets": [
            ("S1", good_852_segments())]}])
        result = _receive(raw, self.root)
        stored_path = Path(self.root) / "raw" / f"{result['raw_sha256']}.x12"
        self.assertTrue(stored_path.exists())
        self.assertEqual(stored_path.read_bytes(), raw)

    def test_generated_997_is_itself_valid_parseable_x12(self):
        raw = build_interchange(groups=[{"control": "G1", "sets": [
            ("S1", good_852_segments()),
            ("S2", good_852_segments(sku="G21")),
        ]}]).replace(b"SE*6*S2~", b"SE*9*S2~", 1)
        result = _receive(raw, self.root)
        for ack in result["acknowledgments"]:
            ack_interchange = parse(ack["text"].encode("latin-1"))
            self.assertTrue(ack_interchange.ok,
                            [e.message for e in ack_interchange.errors])
            ack_group = ack_interchange.groups[0]
            self.assertEqual(ack_group.func_id, "FA")
            ack_set = ack_group.sets[0]
            self.assertEqual(ack_set.set_type, "997")
            self.assertTrue(ack_set.ok)
            tags = [s.tag for s in ack_set.segments]
            self.assertEqual(tags.count("AK2"), 2)
            self.assertIn("AK9", tags)
            # 回读后信封计数全部自洽（解析器已核对 SE/GE/IEA，能到这里即通过）。


if __name__ == "__main__":
    unittest.main()
