"""关账版本号：同一对账批次 + 同一份期末余额表得到同一个 close_id。

幂等语义：同一组输入重复关账命中同一版本，先删后插，不会产生第二份差额；
余额表或批次变化才会生成新版本，旧版本原样保留可查。
"""
from __future__ import annotations

import hashlib


def make_close_id(run_id: str, balance_sha: str, rule_version: str) -> str:
    payload = "|".join([rule_version, run_id or "-", balance_sha or "-"])
    return "close-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
