"""采集入口。

旧 JSON 单响应流程保持不变（``python -m app.service fixtures/sample.json``）。
新增 X12 原文接收：

    python -m app.service receive --partner dealer-a --file path/to/852.x12 --store var/ledger
    python -m app.service view    --partner dealer-a --store var/ledger

伙伴身份由传输配置（--partner）给出；同一进程内重投与进程重启都回放同一份仅追加日志。
"""
import json
import sys
from pathlib import Path

from .edi import receive_raw
from .source import fetch, from_packet
from .extract import extract
from .ledger import receipt
from .store import Store

KEY = 'transaction_id'


def inspect(packet):
    response = from_packet(packet)
    return receipt(response, extract(response, KEY))


def collect_one(url, source_id):
    response = fetch(url, source_id)
    return receipt(response, extract(response, KEY))


def receive_file(store_root: str, partner_id: str, x12_path: str) -> dict:
    raw = Path(x12_path).read_bytes()
    store = Store(store_root, partner_id)
    with store.locked():
        return receive_raw(store, raw)


def current_view(store_root: str, partner_id: str) -> dict:
    store = Store(store_root, partner_id)
    with store.locked():
        return {"partner_id": partner_id, "quantities": store.current_quantities()}


def _usage() -> None:
    raise SystemExit(
        "用法：\n"
        "  python -m app.service <json-响应文件>            旧 JSON 流程\n"
        "  python -m app.service receive --partner P --file F --store D\n"
        "  python -m app.service view --partner P --store D"
    )


def main():
    args = sys.argv[1:]
    if not args:
        _usage()

    if args[0] in ("receive", "view"):
        options = {}
        i = 1
        while i < len(args):
            if args[i].startswith("--") and i + 1 < len(args):
                options[args[i][2:]] = args[i + 1]
                i += 2
            else:
                _usage()
        missing = {"partner", "store"} - options.keys()
        if missing or (args[0] == "receive" and "file" not in options):
            _usage()
        if args[0] == "receive":
            result = receive_file(options["store"], options["partner"], options["file"])
        else:
            result = current_view(options["store"], options["partner"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if len(args) != 1:
        _usage()
    packet = json.loads(Path(args[0]).read_text(encoding='utf-8'))
    print(json.dumps(inspect(packet), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
