# 经销商 EDI 852 的控制号边界、确认与版本化接收

接收器直接接收 X12 004010 原文（不再只看 JSON 摘要），按交换/功能组/交易集三层控制号
保留边界，逐交易集给出接受或拒绝结论，并生成发送方可核对的 997 功能确认。项目只使用
Python 标准库，不接触真实企业系统，样例均为合成数据。

## 模块

| 模块 | 职责 |
| --- | --- |
| `app/x12.py` | 从 ISA 识别分隔符（不假定字符）；ISA/GS/ST 与 IEA/GE/SE 控制号、计数核对；分层错误与字节偏移 |
| `app/activity.py` | 852 提取：LIN 选 SK、QTY 数量、N1 地点、REF*CR 修正引用；每条数量带来源位置 |
| `app/ack997.py` | 按组生成 997，AK2/AK5 逐单据反映接受/拒绝，控制号确定性派生 |
| `app/store.py` | 仅追加事件日志 + 内容寻址原件；幂等重投、冲突拒收、修订单版本链、重启重放 |
| `app/edi.py` | 编排：原文 → 解析 → 提取 → 997 → 台账收据 |
| `app/service.py` | CLI 与旧 JSON 流程入口（旧流程保持不变） |
| `app/source.py` `app/extract.py` `app/ledger.py` | 既有单响应 JSON 流程，未改动语义 |

字段与语义约定见 [docs/source-contract.md](docs/source-contract.md)。

## 命令行

旧 JSON 流程不变：

    python -m app.service fixtures/sample.json

接收一份 X12 原文（伙伴身份由传输配置 `--partner` 给出）：

    python -m app.service receive --partner dealer-a --file path/to/852.x12 --store var/ledger

查看当前有效数量（只含修订单版本链链头，不做跨版本相加）：

    python -m app.service view --partner dealer-a --store var/ledger

处理不可信来源文件时用隔离模式（`-I` 不读环境变量与用户 site 包）；隔离模式不把当前
目录加入 `sys.path`，需显式指定包所在目录后再运行测试，CLI 则在普通模式下运行、由文件
系统权限与仅追加台账控制写入面：

    python -I -m unittest discover -s tests -t . -v

## 关键保证

* 一个交易集格式错误只拒绝该单据，同批可用单据照常接受，997 的 AK9 用 `P` 表示部分接受。
* 同伙伴同控制号、字节相同 → 重投返回同一收据与同一 997；字节不同 → 冲突拒收且不覆盖。
* 控制号按伙伴隔离，伙伴之间重用号码互不影响。
* 修订单经 `REF*CR` 关联原单，整单取代而非数量相加，历史版本可追溯。
* 原件内容寻址落盘，事件仅追加并 fsync；断线重投与进程重启维持同一接收结论。

## 测试

    python -I -m unittest discover -s tests -t . -v

检查编译：

    python -I -m compileall -q app tests