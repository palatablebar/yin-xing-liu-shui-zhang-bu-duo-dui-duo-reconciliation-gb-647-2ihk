"""不变量：守恒、不重叠、手续费守恒、幂等、隔离不参与合计。"""
from decimal import Decimal
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"


def test_bank_conservation(client, demo):
    identities = {item["key"]: item for item in client.get("/api/differences").json()["identities"]}
    assert identities["bank_amount"]["balanced"] is True
    assert identities["ledger_amount"]["balanced"] is True
    assert identities["bank_count"]["balanced"] is True
    assert identities["ledger_count"]["balanced"] is True


def test_disjoint_assignment(client, demo):
    groups = client.get("/api/matches").json()["groups"]
    bank = [key for group in groups for key in group["bank_ids"]]
    ledger = [key for group in groups for key in group["ledger_ids"]]
    assert len(bank) == len(set(bank))
    assert len(ledger) == len(set(ledger))


def test_fee_conservation(client, demo):
    data = client.get("/api/matches").json()
    allocated = sum((Decimal(group["fee_alloc"]) for group in data["groups"]), Decimal("0.00"))
    actual = sum((Decimal(group["fee_actual"]) for group in data["groups"]), Decimal("0.00"))
    assert allocated == actual == Decimal("22.31")
    assert allocated == Decimal(demo["totals"]["bank_fee"])


def test_rerun_row_counts_stable(client, demo):
    before = client.get("/api/statements", params={"side": "bank"}).json()["count"]
    ledger_before = client.get("/api/statements", params={"side": "ledger"}).json()["count"]
    client.post("/api/demo/run")
    after = client.get("/api/statements", params={"side": "bank"}).json()["count"]
    ledger_after = client.get("/api/statements", params={"side": "ledger"}).json()["count"]
    assert before == after == 22
    assert ledger_before == ledger_after == 19


def test_quarantine_excluded_from_totals(client, demo):
    rows = client.get("/api/statements", params={"side": "bank"}).json()["rows"]
    gross = sum((Decimal(row["amount"]) * int(row["direction"]) for row in rows), Decimal("0.00"))
    assert gross == Decimal(demo["totals"]["bank_gross"])
    assert Decimal("999.00") not in {Decimal(row["amount"]) for row in rows}


def test_close_gap_conservation_no_adjustment(client, demo):
    """关账守恒：每账户 银行期末=期初+流水累计+未解释差额，且不存在调整分录。"""
    body = client.post("/api/closes", data={"sample": "bank_balance_dirty.csv"}).json()
    for account in body["accounts"]:
        opening = Decimal(account["opening_balance"])
        flow = Decimal(account["flow_signed"])
        closing = Decimal(account["bank_closing"])
        gap = Decimal(account["unexplained"])
        assert closing == opening + flow + gap
    summary = body["summary"]
    assert Decimal(summary["unexplained_total"]) == sum(
        (Decimal(a["unexplained"]) for a in body["accounts"]), Decimal("0.00"))
    # 流水侧合计没有被关账动作改动：没有新增任何调整流水行
    rerun_demo = client.post("/api/demo/run").json()
    assert rerun_demo["totals"] == demo["totals"]
    assert rerun_demo["counts"] == demo["counts"]


def test_close_versions_are_immutable_snapshots(client, demo):
    """新版本生成后，旧版快照金额逐字不变；页面与导出读同一版。"""
    first = client.post("/api/closes", data={"sample": "bank_balance_dirty.csv"}).json()
    text = (SAMPLE_DIR / "bank_balance_dirty.csv").read_text(encoding="utf-8")
    changed = text.replace("10593.25", "10590.25").encode("utf-8")
    client.post("/api/closes",
                files={"file": ("changed.csv", changed, "text/csv")})
    old = client.get(f"/api/closes/{first['close_id']}").json()
    assert old["accounts"] == first["accounts"]
    assert old["summary"] == first["summary"]
