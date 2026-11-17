"""银行期末余额表解析：解码 → 列映射 → 逐行校验。

期末余额是关账权威值，单独成表，不复用流水采信管线：
- 列名走别名表，支持「期初余额/期末余额」等常见表头；
- 金额复用 M2 的 Decimal 归一（千分位、全角、括号负数、空值哨兵）；
- 账户必须在登记册内，未登记、空账户、金额无法解析逐行诊断，不静默丢弃；
- 同账户重复行直接报错，避免后写覆盖让期末余额随文件顺序变化。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal

from ..m1_ingest.decode import decode_bytes, normalize_cell
from ..m2_contract.coerce import parse_amount

ACCOUNT_ALIASES = ("账户", "账号", "账户号", "银行账号", "account", "acct")
OPENING_ALIASES = ("期初余额", "期初", "上月余额", "opening", "opening_balance", "begin_balance")
CLOSING_ALIASES = ("期末余额", "期末", "账面余额", "银行余额", "closing", "closing_balance",
                   "end_balance", "balance")
NAME_ALIASES = ("账户名称", "户名", "名称", "account_name", "name")

REQUIRED = (ACCOUNT_ALIASES, OPENING_ALIASES, CLOSING_ALIASES)


@dataclass
class BalanceRow:
    row_no: int
    account: str
    account_name: str
    opening: Decimal
    closing: Decimal
    raw: dict = field(default_factory=dict)


@dataclass
class BalanceIssue:
    row_no: int
    code: str
    message: str
    raw: dict = field(default_factory=dict)


@dataclass
class BalanceFile:
    encoding: str | None
    bom: bool
    header: list[str]
    rows: list[BalanceRow]
    issues: list[BalanceIssue]
    blank_rows: int


def _header_index(header: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for pos, cell in enumerate(header):
        key = normalize_cell(cell).lower()
        if key and key not in index:
            index[key] = pos
    return index


def _pick(index: dict[str, int], aliases: tuple[str, ...]) -> int | None:
    for alias in aliases:
        pos = index.get(alias.lower())
        if pos is not None:
            return pos
    return None


def parse_balance(raw: bytes, settings) -> BalanceFile:
    decoded = decode_bytes(raw, settings.encodings)
    if decoded["error"] == "EMPTY_FILE":
        return BalanceFile(decoded["encoding"], decoded["bom"], [], [], [], 0)

    reader = csv.reader(io.StringIO(decoded["text"], newline=""))
    parsed = list(reader)
    if not parsed:
        return BalanceFile(decoded["encoding"], decoded["bom"], [], [], [], 0)

    header = [normalize_cell(cell) for cell in parsed[0]]
    index = _header_index(header)
    missing = [aliases[0] for aliases in REQUIRED if _pick(index, aliases) is None]
    if missing:
        raise ValueError(f"期末余额表缺少必填列：{'、'.join(missing)}")

    account_pos = _pick(index, ACCOUNT_ALIASES)
    opening_pos = _pick(index, OPENING_ALIASES)
    closing_pos = _pick(index, CLOSING_ALIASES)
    name_pos = _pick(index, NAME_ALIASES)

    rows: list[BalanceRow] = []
    issues: list[BalanceIssue] = []
    blank_rows = 0
    seen: set[str] = set()
    registered = settings.accounts

    for line_no, cells in enumerate(parsed[1:], start=2):
        norm = [normalize_cell(cell) for cell in cells]
        if not any(norm):
            blank_rows += 1
            continue
        raw_row = {header[i] if i < len(header) else f"列{i+1}": cells[i] if i < len(cells) else ""
                   for i in range(max(len(header), len(cells)))}

        def cell(pos: int | None) -> str:
            return norm[pos] if pos is not None and pos < len(norm) else ""

        account = cell(account_pos)
        if not account:
            issues.append(BalanceIssue(line_no, "BALANCE_ACCOUNT_EMPTY", "账户为空", raw_row))
            continue
        if account not in registered:
            issues.append(BalanceIssue(
                line_no, "BALANCE_ACCOUNT_UNKNOWN", f"账户未登记：{account}", raw_row))
            continue
        if account in seen:
            issues.append(BalanceIssue(
                line_no, "BALANCE_DUP_ACCOUNT", f"账户重复出现：{account}", raw_row))
            continue

        try:
            opening = parse_amount(cell(opening_pos), settings.sentinels)
            closing = parse_amount(cell(closing_pos), settings.sentinels)
        except ValueError:
            issues.append(BalanceIssue(
                line_no, "BALANCE_AMOUNT_EMPTY", "期初或期末余额为空值/无法解析", raw_row))
            continue
        if opening is None or closing is None:
            issues.append(BalanceIssue(
                line_no, "BALANCE_AMOUNT_EMPTY", "期初或期末余额为空值/无法解析", raw_row))
            continue

        seen.add(account)
        rows.append(BalanceRow(
            row_no=line_no,
            account=account,
            account_name=cell(name_pos) or registered.get(account, ""),
            opening=opening,
            closing=closing,
            raw=raw_row,
        ))

    return BalanceFile(
        encoding=decoded["encoding"],
        bom=decoded["bom"],
        header=header,
        rows=rows,
        issues=issues,
        blank_rows=blank_rows,
    )
