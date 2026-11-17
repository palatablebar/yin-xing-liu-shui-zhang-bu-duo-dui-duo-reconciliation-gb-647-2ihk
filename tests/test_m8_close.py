"""M8 月结关账：关账口径、未解释差额、版本快照、幂等与失败保留旧版。"""
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

from app.modules.m8_close import balance_reader, closing, versioning

BALANCE = "bank_balance_dirty.csv"
SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
QUANT = Decimal("0.01")


def _sample_bytes(name: str = BALANCE) -> bytes:
    return (SAMPLE_DIR / name).read_bytes()


# ---------- 期末余额表解析 ----------

def test_parse_balance_three_accounts(settings):
    raw = _sample_bytes()
    parsed = balance_reader.parse_balance(raw, settings)
    assert parsed.issues == []
    assert [row.account for row in parsed.rows] == [
        "6222-0012", "6222-0034", "6222-0056"]
    assert parsed.rows[0].opening == Decimal("500000.00")
    assert parsed.rows[0].closing == Decimal("255893.01")


def test_parse_balance_rejects_unknown_and_dup_account(settings):
    raw = (
        "账户,期初余额,期末余额\n"
        "6222-9999,10.00,20.00\n"
        "6222-0012,10.00,20.00\n"
        "6222-0012,10.00,20.00\n"
        ",1.00,2.00\n"
    ).encode("utf-8")
    parsed = balance_reader.parse_balance(raw, settings)
    codes = {issue.code for issue in parsed.issues}
    assert codes == {
        "BALANCE_ACCOUNT_UNKNOWN", "BALANCE_DUP_ACCOUNT", "BALANCE_ACCOUNT_EMPTY"}
    assert [row.account for row in parsed.rows] == ["6222-0012"]


def test_parse_balance_amount_sentinel_and_unparseable(settings):
    raw = (
        "账户,期初余额,期末余额\n"
        "6222-0012,N/A,20.00\n"
        "6222-0034,10.00,abc\n"
    ).encode("utf-8")
    parsed = balance_reader.parse_balance(raw, settings)
    assert [issue.code for issue in parsed.issues] == [
        "BALANCE_AMOUNT_EMPTY", "BALANCE_AMOUNT_EMPTY"]
    assert parsed.rows == []


def test_parse_balance_missing_column(settings):
    raw = "账户,期末余额\n6222-0012,20.00\n".encode("utf-8")
    with pytest.raises(ValueError, match="期初余额"):
        balance_reader.parse_balance(raw, settings)


def test_parse_balance_fullwidth_and_thousands(settings):
    raw = "账户,期初余额,期末余额\n6222-0012,５００，０００．００,\"255,893.01\"\n".encode("utf-8")
    parsed = balance_reader.parse_balance(raw, settings)
    assert parsed.rows[0].opening == Decimal("500000.00")
    assert parsed.rows[0].closing == Decimal("255893.01")


def test_parse_balance_header_aliases(settings):
    raw = "账号,上月余额,账面余额\n6222-0012,1.00,2.00\n".encode("utf-8")
    parsed = balance_reader.parse_balance(raw, settings)
    assert len(parsed.rows) == 1
    assert parsed.rows[0].closing == Decimal("2.00")


# ---------- 关账计算（纯函数） ----------

def test_unexplained_gap_formula(client, demo):
    store = client.app.state.store
    run = store.latest_run()
    raw = _sample_bytes()
    parsed = balance_reader.parse_balance(raw, client.app.state.settings)
    accounts = closing.build_accounts(parsed.rows, store.records(run["run_id"]),
                                      client.app.state.settings)
    by_account = {a["account"]: a for a in accounts}
    # 隔日漏入账手续费：银行期末比推算期末少 6.00
    assert by_account["6222-0012"]["unexplained"] == Decimal("-6.00")
    assert by_account["6222-0034"]["unexplained"] == Decimal("-6.00")
    # 完全对平的账户差额为 0
    assert by_account["6222-0056"]["unexplained"] == Decimal("0.00")
    assert by_account["6222-0056"]["has_gap"] is False
    for account in accounts:
        assert account["unexplained"] == (
            account["bank_closing"] - account["expected_closing"]).quantize(QUANT)
        assert account["expected_closing"] == (
            account["opening_balance"] + account["flow_signed"]).quantize(QUANT)


def test_closing_value_is_bank_balance_not_flow(client, demo):
    """关账现金取银行期末，绝不用流水累计推算值覆盖。"""
    body = client.post("/api/closes", data={"sample": BALANCE}).json()
    summary = body["summary"]
    assert Decimal(summary["closing_total"]) == Decimal("268708.49")
    assert Decimal(summary["expected_total"]) == Decimal("268720.49")
    assert Decimal(summary["unexplained_total"]) == Decimal("-12.00")
    # 关账总额 ≠ 推算期末，证明没有拿流水合计当关账值
    assert summary["closing_total"] != summary["expected_total"]


def test_quarantine_rows_excluded_from_flow(client, demo):
    store = client.app.state.store
    run = store.latest_run()
    raw = _sample_bytes()
    parsed = balance_reader.parse_balance(raw, client.app.state.settings)
    accounts = {a["account"]: a for a in closing.build_accounts(
        parsed.rows, store.records(run["run_id"]), client.app.state.settings)}
    # 工作台口径的银行有效流水累计（22 笔有效行）
    valid = [r for r in store.records(run["run_id"], side="bank")
             if r["status"] == "valid" and r["account"] == "6222-0012"]
    flow = sum((Decimal(r["amount"]) * r["direction"] for r in valid), Decimal("0.00"))
    assert accounts["6222-0012"]["flow_signed"] == flow.quantize(QUANT)
    assert accounts["6222-0012"]["flow_count"] == len(valid) == 11


# ---------- API 与版本快照 ----------

def test_create_close_and_get_latest(client, demo):
    created = client.post("/api/closes", data={"sample": BALANCE, "period": "2026-03"})
    assert created.status_code == 200
    close_id = created.json()["close_id"]
    latest = client.get("/api/closes/latest").json()
    assert latest["close_id"] == close_id
    assert latest["version"]["period"] == "2026-03"
    assert latest["version"]["unexplained_count"] == 2
    assert len(latest["accounts"]) == 3
    # 快照里冻结了来源批次与余额表指纹
    assert len(latest["balance_sha256"]) == 64
    assert latest["source_sha"]["bank_sha256"]


def test_close_requires_matched_run(client):
    # 全新应用，没有批次 -> 409 而不是静默生成
    response = client.post("/api/closes", data={"sample": BALANCE})
    assert response.status_code == 409
    assert response.json()["error"] == "E_NO_RUN"
    assert client.get("/api/closes/latest").status_code == 404


def test_bad_balance_file_rejected_without_version(client, demo):
    bad = (
        "账户,期初余额,期末余额\n"
        "6222-9999,10.00,20.00\n"
    ).encode("utf-8")
    response = client.post("/api/closes",
                           files={"file": ("bad.csv", bad, "text/csv")})
    assert response.status_code == 400
    assert response.json()["error"] == "BALANCE_ACCOUNT_UNKNOWN"
    assert client.get("/api/closes").json()["count"] == 0


def test_rerun_same_inputs_is_idempotent(client, demo):
    first = client.post("/api/closes", data={"sample": BALANCE}).json()
    second = client.post("/api/closes", data={"sample": BALANCE}).json()
    assert first["close_id"] == second["close_id"]
    assert client.get("/api/closes").json()["count"] == 1
    # 同一批重跑不能重复生成差额：合计与逐户差额完全一致
    assert first["summary"] == second["summary"]
    assert first["accounts"] == second["accounts"]


def test_changed_balance_creates_new_version_keeps_old(client, demo):
    first = client.post("/api/closes", data={"sample": BALANCE}).json()
    text = (SAMPLE_DIR / BALANCE).read_text(encoding="utf-8")
    changed = text.replace("255893.01", "255890.01").encode("utf-8")
    second = client.post(
        "/api/closes", files={"file": ("changed.csv", changed, "text/csv")}).json()
    assert second["close_id"] != first["close_id"]
    versions = client.get("/api/closes").json()["versions"]
    assert [v["close_id"] for v in versions] == [second["close_id"], first["close_id"]]
    # 旧版完整可读
    old = client.get(f"/api/closes/{first['close_id']}").json()
    assert old["summary"]["closing_total"] == first["summary"]["closing_total"]
    # 最新版指向新版
    assert client.get("/api/closes/latest").json()["close_id"] == second["close_id"]


def test_failed_rerun_keeps_old_version_intact(client, demo):
    created = client.post("/api/closes", data={"sample": BALANCE}).json()
    good_id = created["close_id"]
    store = client.app.state.store
    real_cm = store._conn
    armed = {"on": False}

    class ConnProxy:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def executemany(self, sql, parameters):
            if armed["on"] and "INSERT INTO close_accounts" in sql:
                raise sqlite3.OperationalError("injected disk failure")
            return self._conn.executemany(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def flaky_conn():
        conn = sqlite3.connect(store.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield ConnProxy(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    store._conn = flaky_conn
    armed["on"] = True
    with pytest.raises(sqlite3.OperationalError):
        client.post("/api/closes", data={"sample": BALANCE})
    armed["on"] = False
    store._conn = real_cm

    # 旧版与账户行完整保留，没有半成品版本
    assert client.get("/api/closes").json()["count"] == 1
    latest = client.get("/api/closes/latest").json()
    assert latest["close_id"] == good_id
    assert len(store.close_accounts(good_id)) == 3
    # 恢复后重跑成功，仍是同一个幂等版本
    again = client.post("/api/closes", data={"sample": BALANCE}).json()
    assert again["close_id"] == good_id


# ---------- 页面与导出同一版结果 ----------

def test_page_csv_json_share_one_snapshot(client, demo):
    body = client.post("/api/closes", data={"sample": BALANCE}).json()
    close_id = body["close_id"]
    page = client.get("/close")
    assert page.status_code == 200
    assert close_id in page.text
    csv_text = client.get(f"/api/closes/latest/close.csv?version={close_id}").text
    json_text = client.get(f"/api/closes/latest/close.json?version={close_id}").text
    snapshot = json.loads(json_text)
    # 三处的关账总额与未解释差额完全一致
    assert body["summary"]["closing_total"] in csv_text
    assert body["summary"]["unexplained_total"] in csv_text
    assert snapshot["close_id"] == close_id
    assert snapshot["summary"] == body["summary"]
    # 导出载荷不含重复明细，账户行只保留快照一份
    assert "stored_accounts" not in snapshot
    # 逐账户行都出现在 CSV，金额字符串不做 float 转换
    for account in body["accounts"]:
        assert account["account"] in csv_text
        assert account["unexplained"] in csv_text


def test_page_switches_between_versions_and_exports_same_one(client, demo):
    first = client.post("/api/closes", data={"sample": BALANCE}).json()
    text = (SAMPLE_DIR / BALANCE).read_text(encoding="utf-8")
    changed = text.replace("255893.01", "255890.01").encode("utf-8")
    second = client.post(
        "/api/closes", files={"file": ("changed.csv", changed, "text/csv")}).json()

    latest_page = client.get("/close").text
    assert second["close_id"] in latest_page
    old_page = client.get(f"/close?version={first['close_id']}").text
    assert first["close_id"] in old_page
    assert "返回最新版本" in old_page
    # 旧版页面联动导出旧版，不能串到最新版
    old_csv = client.get(
        f"/api/closes/latest/close.csv?version={first['close_id']}").text
    assert first["summary"]["closing_total"] in old_csv
    assert second["summary"]["closing_total"] not in old_csv
    # 不存在的版本返回 404
    assert client.get("/close?version=close-notexist").status_code == 404


def test_csv_has_total_row_and_policy_note(client, demo):
    client.post("/api/closes", data={"sample": BALANCE})
    text = client.get("/api/closes/latest/close.csv").text.lstrip("﻿")
    assert "合计" in text
    assert "未解释差额" in text
    assert "不做调整分录" in text
    header = text.splitlines()[0]
    assert header.startswith("关账版本,会计期间,账户")


def test_close_json_is_sorted_serializable(client, demo):
    client.post("/api/closes", data={"sample": BALANCE})
    body = json.loads(client.get("/api/closes/latest/close.json").text)
    assert body["status"] == "closed"
    assert body["version"]["closing_total"] == "268708.49"


def test_history_endpoint_lists_versions(client, demo):
    client.post("/api/closes", data={"sample": BALANCE})
    history = client.get("/api/closes").json()
    assert history["count"] == 1
    item = history["versions"][0]
    assert item["unexplained_total"] == "-12.00"
    assert item["account_count"] == 3


def test_close_id_deterministic():
    cid = versioning.make_close_id("run-x", "a" * 64, "2026.11.1")
    assert cid == versioning.make_close_id("run-x", "a" * 64, "2026.11.1")
    assert cid != versioning.make_close_id("run-y", "a" * 64, "2026.11.1")
    assert cid.startswith("close-")


def test_account_without_flow_keeps_full_gap(settings):
    """余额表有、当月没有任何有效流水的账户：流水累计为 0，差额完整保留。"""
    from app.modules.m8_close.balance_reader import BalanceRow

    rows = [BalanceRow(
        row_no=2, account="6222-0012", account_name="基本户",
        opening=Decimal("100.00"), closing=Decimal("90.00"))]
    accounts = closing.build_accounts(rows, [], settings)
    only = accounts[0]
    assert only["flow_signed"] == Decimal("0.00")
    assert only["flow_count"] == 0
    assert only["expected_closing"] == Decimal("100.00")
    assert only["unexplained"] == Decimal("-10.00")
    assert only["has_gap"] is True
    summary = closing.summarize(accounts)
    assert summary["closing_total"] == Decimal("90.00")
    assert summary["unexplained_total"] == Decimal("-10.00")


def test_balance_empty_and_header_only_rejected(client, demo):
    for raw in (b"", "账户,期初余额,期末余额\n".encode("utf-8")):
        response = client.post(
            "/api/closes", files={"file": ("empty.csv", raw, "text/csv")})
        assert response.status_code == 400
        assert response.json()["error"] == "E_BALANCE_EMPTY"
    assert client.get("/api/closes").json()["count"] == 0


def test_balance_gb18030_and_bom_supported(client, demo):
    raw_gbk = "账户,期初余额,期末余额\n6222-0012,1.00,2.00\n".encode("gb18030")
    raw_bom = "﻿账户,期初余额,期末余额\n6222-0012,1.00,2.00\n".encode("utf-8")
    for raw in (raw_gbk, raw_bom):
        response = client.post(
            "/api/closes", files={"file": ("b.csv", raw, "text/csv")})
        assert response.status_code == 200
        assert response.json()["accounts"][0]["bank_closing"] == "2.00"
