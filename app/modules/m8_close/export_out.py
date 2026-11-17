"""关账导出：逐账户 CSV 与版本快照 JSON，数据全部来自已落库的同一版结果。"""
from __future__ import annotations

import csv
import io
import json

CLOSE_HEADER = [
    "关账版本", "会计期间", "账户", "账户名称", "期初余额", "流水累计(带符号)",
    "流水笔数", "其中手续费", "推算期末", "银行期末(关账值)", "未解释差额",
]


def close_csv_text(payload: dict) -> str:
    version = payload["version"]
    summary = payload["summary"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CLOSE_HEADER)
    for account in payload["accounts"]:
        writer.writerow([
            version["close_id"],
            version["period"],
            account["account"],
            account["account_name"],
            account["opening_balance"],
            account["flow_signed"],
            account["flow_count"],
            account["fee_total"],
            account["expected_closing"],
            account["bank_closing"],
            account["unexplained"],
        ])
    writer.writerow([])
    writer.writerow([
        "合计", version["period"], "", "",
        summary["opening_total"], summary["flow_total"], "", summary["fee_total"],
        summary["expected_total"], summary["closing_total"], summary["unexplained_total"],
    ])
    writer.writerow([])
    writer.writerow(["关账口径", "关账现金取银行期末余额；未解释差额=银行期末-(期初+流水累计)，"
                                "逐户保留，不做调整分录"])
    return "\ufeff" + buffer.getvalue()


def close_json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
