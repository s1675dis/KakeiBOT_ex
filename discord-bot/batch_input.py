"""日付範囲を指定した複数行の支出入力。"""

import math
import re
from datetime import date, timedelta

from config import Config

DEFAULT_CATEGORY = "食費"
_HEADER = re.compile(r"!入力[^\S\r\n]+([0-9]{6})-([0-9]{6})")
_ENTRY = re.compile(r"([0-9]+(?:\.[0-9]+)?)(?:\s+(.+))?")


def parse_batch_expenses(
    content: str, default_currency: str = Config.DEFAULT_CURRENCY
) -> list[tuple[date, float, str, str]]:
    """初日から記録し、金額間の空行のまとまりで翌日に進む。"""
    lines = content.strip().splitlines()
    header = _HEADER.fullmatch(lines[0].strip()) if lines else None
    if header is None:
        raise ValueError("1行目を !入力 YYMMDD-YYMMDD とし、2行目以降に金額 [カテゴリ] を入力してください。")

    def parse_date(value: str) -> date:
        try:
            return date(2000 + int(value[:2]), int(value[2:4]), int(value[4:]))
        except ValueError:
            raise ValueError("実在する日付をYYMMDD形式で指定してください。") from None

    start, end = (parse_date(value) for value in header.groups())
    if end < start:
        raise ValueError("終了日は開始日以降にしてください。")

    entries = []
    day = start
    next_day = False
    for line_number, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line:
            if entries:
                next_day = True
            continue
        if next_day:
            day += timedelta(days=1)
            next_day = False
        if day > end:
            raise ValueError(f"{line_number}行目が指定した終了日を超えています。空行で翌日に進みます。")
        match = _ENTRY.fullmatch(line)
        if match is None:
            raise ValueError(f"{line_number}行目が不正です。0以上の金額 [カテゴリ] を入力してください。一括入力では取消はできません。")
        amount = float(match.group(1))
        if not math.isfinite(amount):
            raise ValueError(f"{line_number}行目の金額が大きすぎます。")
        category = (match.group(2) or "").strip()
        currency = default_currency.upper()
        parts = category.rsplit(None, 1)
        if parts and parts[-1].upper() in Config.SUPPORTED_CURRENCIES:
            currency = parts[-1].upper()
            category = parts[0] if len(parts) == 2 else ""
        entries.append((day, amount, category or DEFAULT_CATEGORY, currency))
    if not entries:
        raise ValueError("コマンドの次の行に金額を入力してください。")
    return entries
