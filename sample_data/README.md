# 样例数据

- `bank_statement_dirty.csv`：带 BOM、重复表头、千分位、空值、符号错、日期错、未知账户、重复流水号、多余列与超 2^53 编号。
- `ledger_book_dirty.csv`：GB18030 编码、中文列名、日期与时间混用、重复凭证号冲突。
- `bank_truncated_tail.csv`：末行没有换行且列数不足。
- `empty_bank.csv`、`header_only_ledger.csv`、`all_blank_bank.csv`：三种空态。
- `bank_balance_dirty.csv`：月结期末余额表（3 个登记账户的期初/期末）。其中基本户与一般户的期末比「期初+流水累计」各少 6.00 元，模拟隔日入账、流水漏掉的手续费；货款户完全对平。
