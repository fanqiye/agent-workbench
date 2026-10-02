# 代码与资料来源

- `vendor/providers.py` 原样复用自本人项目 [commerce-case-agent](https://github.com/fanqiye/commerce-case-agent/tree/98561bb3617a0835a2959d6a1efa323f3c0e21af)。保留原厂适配代码，但本工作台的执行入口只接受 Ollama，不进行原厂调用。
- `vendor/gateway.py` 与 `examples/documents.json` 原样复用自本人项目 [support-evidence-gateway](https://github.com/fanqiye/support-evidence-gateway/tree/1c9b1048adf71e5efce0007efd252dc72adf5e0b)。没有重写权限检索服务。新代码在其上增加工具调用、引用核对与生成草稿。
- 操作系统文件锁的实现沿用 commerce-case-agent 的本机单写入模式。新工作台中断后转人工，不声称支持其完整任务租约与恢复功能。
- 销售 CSV 与个人安排均为本仓库合成样例，没有真实企业交易或个人行程。
- SQL 限制参考 [Python sqlite3 授权与进度回调文档](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.set_authorizer)。授权器限制读表、函数和操作类型；进度处理器限制虚拟机工作量。
- 日历输出参考 [RFC 5545](https://www.rfc-editor.org/rfc/rfc5545)：UTF-8 文本转义、75 字节折行及 UTC 时间。未实现完整 iCalendar 解析器、重复规则或邀请协议。

开发过程中使用编码助手辅助。公开检查、失败记录和复核步骤用于说明实际实现范围，不代表独立外部审计或生产使用。
