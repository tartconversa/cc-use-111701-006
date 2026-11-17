# 来源契约

授权伙伴发送 X12 004010 的 ISA/GS/ST 852；ISA 定义元素、组件和段分隔符，IEA/GE/SE 控制号与数量须核对。业务 SKU 位于 LIN，数量位于 QTY；伙伴身份由传输配置给出，不用控制号代替。接收结果按交易集保留接受或拒绝原因，并可生成对应的功能确认原文。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 transaction_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
