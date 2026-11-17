# 来源契约

来源先给 stream_id、version、encoding、total_bytes、segments[{start,end,sha256}]，下载接口支持 If-Range/Range；业务行是压缩后 NDJSON，每行含 event_id、revision、sku、quantity。只有来源原始字节校验后才可解码，分段边界不保证等于行边界。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 event_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
