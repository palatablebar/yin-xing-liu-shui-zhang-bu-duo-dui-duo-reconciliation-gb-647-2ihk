"""月结关账核心：按账户算期初、流水累计、银行期末与未解释差额。

关账口径（财务与审计共同采信）：
- 关账现金 = 银行期末余额（银行对账单权威值），流水累计不覆盖它；
- 未解释差额 = 银行期末 − (期初 + 流水累计)，即流水缺行（如隔日入账手续费）的金额；
- 差额原样保留、逐账户列示，不生成任何一笔调整分录去抹平；
- 金额一律 Decimal，量化到分；隔离行不进流水累计。
"""
from __future__ import annotations

from decimal import Decimal

from ...config import dec
from ..m2_contract.coerce import QUANT

ZERO = Decimal("0.00")


def _signed(record: dict) -> Decimal:
    return dec(record["amount"]) * int(record["direction"])


def flow_by_account(records: list[dict]) -> dict[str, dict]:
    """按账户汇总有效银行流水：带符号发生额、手续费与笔数。"""
    out: dict[str, dict] = {}
    for record in records:
        if record.get("side") != "bank" or record.get("status") != "valid":
            continue
        account = record.get("account", "")
        bucket = out.setdefault(account, {"flow": ZERO, "fee": ZERO, "count": 0})
        bucket["flow"] += _signed(record)
        bucket["fee"] += dec(record.get("fee", "0"))
        bucket["count"] += 1
    return out


def build_accounts(balance_rows, records: list[dict], settings) -> list[dict]:
    """把期末余额表与流水汇总按登记账户对齐，输出逐账户关账行。"""
    flows = flow_by_account(records)
    result: list[dict] = []
    for row in balance_rows:
        account = row.account
        bucket = flows.get(account, {"flow": ZERO, "fee": ZERO, "count": 0})
        opening = row.opening.quantize(QUANT)
        flow = bucket["flow"].quantize(QUANT)
        fee = bucket["fee"].quantize(QUANT)
        bank_closing = row.closing.quantize(QUANT)
        expected = (opening + flow).quantize(QUANT)
        unexplained = (bank_closing - expected).quantize(QUANT)
        result.append({
            "account": account,
            "account_name": row.account_name or settings.accounts.get(account, ""),
            "opening_balance": opening,
            "flow_signed": flow,
            "flow_count": bucket["count"],
            "fee_total": fee,
            "bank_closing": bank_closing,
            "expected_closing": expected,
            "unexplained": unexplained,
            "has_gap": unexplained != ZERO,
        })
    result.sort(key=lambda item: item["account"])
    return result


def summarize(accounts: list[dict]) -> dict:
    """合计行：关账总额取银行期末之和；未解释差额逐户相加，不另造调整。"""
    opening = sum((a["opening_balance"] for a in accounts), ZERO).quantize(QUANT)
    flow = sum((a["flow_signed"] for a in accounts), ZERO).quantize(QUANT)
    fee = sum((a["fee_total"] for a in accounts), ZERO).quantize(QUANT)
    bank_closing = sum((a["bank_closing"] for a in accounts), ZERO).quantize(QUANT)
    expected = sum((a["expected_closing"] for a in accounts), ZERO).quantize(QUANT)
    unexplained = sum((a["unexplained"] for a in accounts), ZERO).quantize(QUANT)
    gap_accounts = [a["account"] for a in accounts if a["has_gap"]]
    return {
        "opening_total": opening,
        "flow_total": flow,
        "fee_total": fee,
        "expected_total": expected,
        "closing_total": bank_closing,
        "unexplained_total": unexplained,
        "unexplained_count": len(gap_accounts),
        "gap_accounts": gap_accounts,
        "account_count": len(accounts),
    }
