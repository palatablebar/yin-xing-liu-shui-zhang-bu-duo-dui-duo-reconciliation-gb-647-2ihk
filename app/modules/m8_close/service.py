"""月结关账服务：生成版本快照、读取当前/历史版本、组装页面与导出载荷。

事务边界：所有计算在库外完成，save_close 单事务先删后插；落库失败整体回滚，
已发布的旧版本保持完整可读，页面与导出永远读同一份已落库快照。
"""
from __future__ import annotations

from decimal import Decimal

from ...errors import DomainError
from ...manifest import sha256_hex, utc_now_iso
from . import balance_reader, closing, versioning


def _decimal_strings(value) -> str:
    return format(value, "f") if isinstance(value, Decimal) else value


def _snapshot_payload(version: dict, accounts: list[dict], summary: dict,
                      balance_file: balance_reader.BalanceFile, run: dict) -> dict:
    """页面与导出的唯一来源：整版结果冻结在 result_json 里。"""
    return {
        "close_id": version["close_id"],
        "run_id": version["run_id"],
        "period": version["period"],
        "created_at": version["created_at"],
        "rule_version": version["rule_version"],
        "balance_file": version["balance_file"],
        "balance_sha256": version["balance_sha256"],
        "source_sha": version["source_sha"],
        "status": version["status"],
        "summary": {key: _decimal_strings(value) for key, value in summary.items()
                    if not isinstance(value, list)},
        "gap_accounts": summary["gap_accounts"],
        "accounts": [
            {key: (_decimal_strings(value) if isinstance(value, Decimal) else value)
             for key, value in account.items()}
            for account in accounts
        ],
        "balance_issues": [
            {"row_no": issue.row_no, "code": issue.code, "message": issue.message}
            for issue in balance_file.issues
        ],
        "inputs": {
            "bank_file": run.get("bank_file", ""),
            "ledger_file": run.get("ledger_file", ""),
        },
    }


def create_close(settings, store, filename: str, raw: bytes, period: str | None = None) -> dict:
    run = store.latest_run()
    if not run:
        raise DomainError("E_NO_RUN", "还没有对账批次，先载入流水再关账", 409)
    if run["status"] != "matched":
        raise DomainError("E_RUN_NOT_MATCHED", "对账批次未完成双侧匹配，不能关账", 409)

    balance_file = balance_reader.parse_balance(raw, settings)
    if balance_file.issues:
        first = balance_file.issues[0]
        raise DomainError(first.code, f"第 {first.row_no} 行：{first.message}", 400)
    if not balance_file.rows:
        raise DomainError("E_BALANCE_EMPTY", "期末余额表没有可用账户行", 400)

    records = store.records(run["run_id"])
    accounts = closing.build_accounts(balance_file.rows, records, settings)
    summary = closing.summarize(accounts)
    balance_sha = sha256_hex(raw)
    close_id = versioning.make_close_id(run["run_id"], balance_sha, settings.rule_version)

    version = {
        "close_id": close_id,
        "run_id": run["run_id"],
        "period": period or _period_from_run(run),
        "created_at": utc_now_iso(),
        "rule_version": settings.rule_version,
        "balance_sha256": balance_sha,
        "balance_file": filename,
        "balance_bytes": len(raw),
        "source_sha": {
            "bank_sha256": run.get("bank_sha256", ""),
            "ledger_sha256": run.get("ledger_sha256", ""),
        },
        "account_count": summary["account_count"],
        "unexplained_count": summary["unexplained_count"],
        "closing_total": summary["closing_total"],
        "unexplained_total": summary["unexplained_total"],
        "status": "closed",
    }
    version["result"] = _snapshot_payload(version, accounts, summary, balance_file, run)
    store.save_close(version, accounts)
    return detail_payload(store, close_id)


def _period_from_run(run: dict) -> str:
    created = run.get("created_at", "")
    return created[:7] if len(created) >= 7 else ""


def latest_payload(store) -> dict | None:
    version = store.latest_close()
    if not version:
        return None
    return _hydrate(store, version)


def detail_payload(store, close_id: str) -> dict:
    version = store.get_close(close_id)
    if not version:
        raise DomainError("E_NO_CLOSE", "关账版本不存在", 404)
    return _hydrate(store, version)


def _hydrate(store, version: dict) -> dict:
    """以落库快照为准组装页面与导出载荷；账户明细表仅用于一致性核对。"""
    result = dict(version["result"])
    stored_accounts = store.close_accounts(version["close_id"])
    snapshot_keys = {
        (a["account"], a["opening_balance"], a["flow_signed"], a["bank_closing"],
         a["unexplained"])
        for a in result["accounts"]
    }
    stored_keys = {
        (a["account"], a["opening_balance"], a["flow_signed"], a["bank_closing"],
         a["unexplained"])
        for a in stored_accounts
    }
    if snapshot_keys != stored_keys:
        raise DomainError("E_SNAPSHOT_DRIFT",
                          f"关账版本 {version['close_id']} 快照与账户明细不一致", 500)
    result["version"] = {
        "close_id": version["close_id"],
        "run_id": version["run_id"],
        "period": version["period"],
        "created_at": version["created_at"],
        "status": version["status"],
        "balance_file": version["balance_file"],
        "balance_sha256": version["balance_sha256"],
        "closing_total": version["closing_total"],
        "unexplained_total": version["unexplained_total"],
        "unexplained_count": version["unexplained_count"],
        "account_count": version["account_count"],
    }
    return result


def history_payload(store, limit: int = 20) -> dict:
    versions = store.list_closes(limit)
    return {
        "count": len(versions),
        "versions": [
            {
                "close_id": item["close_id"],
                "run_id": item["run_id"],
                "period": item["period"],
                "created_at": item["created_at"],
                "rule_version": item["rule_version"],
                "balance_file": item["balance_file"],
                "account_count": item["account_count"],
                "unexplained_count": item["unexplained_count"],
                "closing_total": item["closing_total"],
                "unexplained_total": item["unexplained_total"],
                "status": item["status"],
            }
            for item in versions
        ],
    }
