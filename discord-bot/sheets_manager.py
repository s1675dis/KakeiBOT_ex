import calendar
from datetime import date as DateType
from datetime import datetime, timedelta

import gspread
import pytz
from google.oauth2.service_account import Credentials

from config import Config


# スプレッドシートのシート名
SHEET_EXPENSES = "支出記録"
SHEET_BUDGET = "予算設定"
SHEET_INCOME = "収入記録"
SHEET_SUBSCRIPTIONS = "サブスク"

# 通貨ごとの表示設定
CURRENCY_SYMBOL = {"JPY": "¥", "USD": "$"}
CURRENCY_DECIMALS = {"JPY": 0, "USD": 2}


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%Y年%m月%d日")


def _parse_date(value: str) -> DateType | None:
    """Google Sheetsから返る様々な日付形式をdateオブジェクトに変換する。"""
    for fmt_str in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt_str).date()
        except ValueError:
            continue
    return None


def fmt(amount: float, currency: str) -> str:
    """金額を通貨記号付きでフォーマットする。"""
    symbol = CURRENCY_SYMBOL.get(currency, currency)
    decimals = CURRENCY_DECIMALS.get(currency, 2)
    if decimals == 0:
        return f"{symbol}{amount:,.0f}"
    return f"{symbol}{amount:,.{decimals}f}"


def _now() -> datetime:
    return datetime.now(pytz.timezone(Config.TIMEZONE))


def _current_ym() -> str:
    return _now().strftime("%Y-%m")


def _make_date_clamp(year: int, month: int, day: int) -> DateType:
    """月の日数を超えないようにdateを作成する (例: 2月31日 → 2月28日)。"""
    return DateType(year, month, min(day, calendar.monthrange(year, month)[1]))


def _get_pay_period(payday: int, reference: DateType) -> tuple[DateType, DateType]:
    """paydayを起点とした、referenceが属する期間 (start, end) を返す。
    例) payday=15, reference=3/20 → (3/15, 4/14)
        payday=15, reference=3/10 → (2/15, 3/14)
    """
    if reference.day >= payday:
        start = _make_date_clamp(reference.year, reference.month, payday)
    else:
        prev_year  = reference.year if reference.month > 1 else reference.year - 1
        prev_month = reference.month - 1 or 12
        start = _make_date_clamp(prev_year, prev_month, payday)

    next_year  = start.year + (1 if start.month == 12 else 0)
    next_month = start.month % 12 + 1
    end = _make_date_clamp(next_year, next_month, payday) - timedelta(days=1)
    return start, end


def _income_from_records(records: list[dict], currency: str, year_month: str) -> float:
    for row in records:
        if (str(row.get("年月", "")).strip() == year_month
                and str(row.get("通貨", "")).strip().upper() == currency):
            return float(row.get("金額", 0))
    return 0.0


class SheetsManager:
    def __init__(self) -> None:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(
            Config.GOOGLE_CREDENTIALS_FILE, scopes=scopes
        )
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(Config.SPREADSHEET_ID)

    def _expenses_sheet(self) -> gspread.Worksheet:
        return self._spreadsheet.worksheet(SHEET_EXPENSES)

    def _budget_sheet(self) -> gspread.Worksheet:
        return self._spreadsheet.worksheet(SHEET_BUDGET)

    def _income_sheet(self) -> gspread.Worksheet:
        return self._spreadsheet.worksheet(SHEET_INCOME)

    def _subscriptions_sheet(self) -> gspread.Worksheet:
        return self._spreadsheet.worksheet(SHEET_SUBSCRIPTIONS)

    # ------------------------------------------------------------------
    # 書き込み
    # ------------------------------------------------------------------

    def add_expense(self, amount: float, category: str, currency: str = "JPY") -> bool:
        """支出を記録する。成功したら True を返す。"""
        currency = currency.upper()
        if currency not in Config.SUPPORTED_CURRENCIES:
            currency = Config.DEFAULT_CURRENCY
        try:
            now = _now()
            self._expenses_sheet().append_row(
                [now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), category, amount, currency],
                value_input_option="USER_ENTERED",
            )
            return True
        except Exception as exc:
            print(f"[SheetsManager] add_expense error: {exc}")
            return False

    def set_income(self, amount: float, currency: str, year_month: str | None = None) -> tuple[bool, str]:
        """指定年月の収入を記録する（既存行があれば上書き）。
        year_month は "YYYY-MM" 形式。省略時は今月。
        戻り値: (成功フラグ, エラーメッセージ)
        """
        currency = currency.upper()
        if year_month is None:
            year_month = _current_ym()
        try:
            sheet = self._income_sheet()
            records = sheet.get_all_records()
            for i, row in enumerate(records, start=2):  # ヘッダー行を除くため2始まり
                if str(row.get("年月", "")).strip() == year_month and \
                        str(row.get("通貨", "")).strip().upper() == currency:
                    sheet.update(f"B{i}", [[amount]])
                    return True, ""
            sheet.append_row([year_month, amount, currency], value_input_option="USER_ENTERED")
            return True, ""
        except Exception as exc:
            print(f"[SheetsManager] set_income error: {exc}")
            return False, str(exc)

    def get_income(self, currency: str, year_month: str | None = None) -> float:
        """指定年月・通貨の収入を返す。記録がなければ 0.0。"""
        currency = currency.upper()
        if year_month is None:
            year_month = _current_ym()
        try:
            records = self._income_sheet().get_all_records()
            for row in records:
                if str(row.get("年月", "")).strip() == year_month and \
                        str(row.get("通貨", "")).strip().upper() == currency:
                    return float(row.get("金額", 0))
        except Exception as exc:
            print(f"[SheetsManager] get_income error: {exc}")
        return 0.0

    # ------------------------------------------------------------------
    # 読み取り
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 予算管理 (カテゴリ × 期間)
    # 保存形式: 予算_{category}_{period}  period = 日|週|月
    # 後方互換: "食費予算" → 予算_食費_日 として読み込む
    # ------------------------------------------------------------------

    VALID_PERIODS = ("日", "週", "月")

    def get_all_budgets(self) -> dict[str, tuple[float, str]]:
        """全予算を返す。{category: (amount, period)} period=日|週|月"""
        result: dict[str, tuple[float, str]] = {}
        try:
            records = self._budget_sheet().get_all_records()
            for row in records:
                key = str(row.get("項目", "")).strip()
                # 旧形式の食費予算を日次として読み込む
                if key == "食費予算":
                    result["食費"] = (float(row.get("金額", 0)), "日")
                    continue
                if key.startswith("予算_"):
                    parts = key.split("_", 2)   # ["予算", category, period]
                    if len(parts) == 3 and parts[2] in self.VALID_PERIODS:
                        cat, period = parts[1], parts[2]
                        result[cat] = (float(row.get("金額", 0)), period)
        except Exception as exc:
            print(f"[SheetsManager] get_all_budgets error: {exc}")
        return result

    def set_budget(self, category: str, amount: float, period: str) -> tuple[bool, str]:
        """カテゴリの予算を設定する。既存行があれば上書き（旧形式も統一）。"""
        new_key = f"予算_{category}_{period}"
        old_key = "食費予算" if category == "食費" else None
        try:
            sheet = self._budget_sheet()
            records = sheet.get_all_records()
            for i, row in enumerate(records, start=2):
                k = str(row.get("項目", "")).strip()
                # 旧形式の食費予算 or 既存の同カテゴリ予算行を上書き
                if k == new_key or (old_key and k == old_key):
                    sheet.update(f"A{i}", [[new_key]])
                    sheet.update(f"B{i}", [[amount]])
                    return True, ""
            sheet.append_row([new_key, amount], value_input_option="USER_ENTERED")
            return True, ""
        except Exception as exc:
            print(f"[SheetsManager] set_budget error: {exc}")
            return False, str(exc)

    def delete_budget(self, category: str) -> bool:
        """カテゴリの予算を削除する。削除できた場合は True。"""
        try:
            sheet = self._budget_sheet()
            records = sheet.get_all_records()
            for i, row in enumerate(records, start=2):
                k = str(row.get("項目", "")).strip()
                is_old = (category == "食費" and k == "食費予算")
                is_new = k.startswith(f"予算_{category}_")
                if is_old or is_new:
                    sheet.delete_rows(i)
                    return True
        except Exception as exc:
            print(f"[SheetsManager] delete_budget error: {exc}")
        return False

    def get_payday(self) -> int:
        """給料日 (1〜31) を返す。未設定の場合は 1。"""
        try:
            records = self._budget_sheet().get_all_records()
            for row in records:
                if str(row.get("項目", "")).strip() == "給料日":
                    return max(1, min(31, int(row.get("金額", 1))))
        except Exception as exc:
            print(f"[SheetsManager] get_payday error: {exc}")
        return 1

    def set_payday(self, day: int) -> tuple[bool, str]:
        """給料日を設定する。"""
        day = max(1, min(31, day))
        try:
            sheet = self._budget_sheet()
            records = sheet.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get("項目", "")).strip() == "給料日":
                    sheet.update(f"B{i}", [[day]])
                    return True, ""
            sheet.append_row(["給料日", day], value_input_option="USER_ENTERED")
            return True, ""
        except Exception as exc:
            print(f"[SheetsManager] set_payday error: {exc}")
            return False, str(exc)

    def get_default_currency(self) -> str:
        """スプレッドシートに保存されたデフォルト通貨を返す。未設定の場合は Config の値。"""
        try:
            records = self._budget_sheet().get_all_records()
            for row in records:
                if str(row.get("項目", "")).strip() == "デフォルト通貨":
                    val = str(row.get("金額", "")).strip().upper()
                    if val in Config.SUPPORTED_CURRENCIES:
                        return val
        except Exception as exc:
            print(f"[SheetsManager] get_default_currency error: {exc}")
        return Config.DEFAULT_CURRENCY

    def set_default_currency(self, currency: str) -> tuple[bool, str]:
        """デフォルト通貨をスプレッドシートに保存する。"""
        currency = currency.upper()
        try:
            sheet = self._budget_sheet()
            records = sheet.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get("項目", "")).strip() == "デフォルト通貨":
                    sheet.update(f"B{i}", [[currency]])
                    return True, ""
            sheet.append_row(["デフォルト通貨", currency], value_input_option="USER_ENTERED")
            return True, ""
        except Exception as exc:
            print(f"[SheetsManager] set_default_currency error: {exc}")
            return False, str(exc)

    # ------------------------------------------------------------------
    # 定時報告 ON/OFF (日次|週次|月次)
    # 保存形式: 報告_{report_type}  値: 1=ON, 0=OFF
    # ------------------------------------------------------------------

    REPORT_TYPES = ("日次", "週次", "月次")

    def get_report_enabled(self, report_type: str) -> bool:
        """定時報告の有効/無効を返す。未設定の場合は True (有効)。"""
        key = f"報告_{report_type}"
        try:
            records = self._budget_sheet().get_all_records()
            for row in records:
                if str(row.get("項目", "")).strip() == key:
                    return str(row.get("金額", "1")).strip() != "0"
        except Exception as exc:
            print(f"[SheetsManager] get_report_enabled error: {exc}")
        return True

    def set_report_enabled(self, report_type: str, enabled: bool) -> tuple[bool, str]:
        """定時報告の有効/無効をスプレッドシートに保存する。"""
        key = f"報告_{report_type}"
        value = 1 if enabled else 0
        try:
            sheet = self._budget_sheet()
            records = sheet.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get("項目", "")).strip() == key:
                    sheet.update(f"B{i}", [[value]])
                    return True, ""
            sheet.append_row([key, value], value_input_option="USER_ENTERED")
            return True, ""
        except Exception as exc:
            print(f"[SheetsManager] set_report_enabled error: {exc}")
            return False, str(exc)

    def delete_expense(self, amount: float, category: str, currency: str) -> bool:
        """条件に一致する最新の支出行を削除する。見つからなければ False を返す。"""
        try:
            sheet = self._expenses_sheet()
            records = sheet.get_all_records()
            match_row = None
            for i, row in enumerate(records, start=2):
                if (float(row.get("金額", 0)) == amount
                        and str(row.get("カテゴリ", "")).strip() == category
                        and str(row.get("通貨", "")).strip().upper() == currency):
                    match_row = i  # 最後にマッチした行（最新入力）を対象にする
            if match_row is not None:
                sheet.delete_rows(match_row)
                return True
            return False
        except Exception as exc:
            print(f"[SheetsManager] delete_expense error: {exc}")
            return False


    # ------------------------------------------------------------------
    # サブスク管理
    # 保存形式: 金額 | 通貨 | 備考 | 登録日
    # ------------------------------------------------------------------

    def get_all_subscriptions(self) -> list[dict]:
        """登録済みサブスク一覧を返す。"""
        try:
            return self._subscriptions_sheet().get_all_records()
        except Exception as exc:
            print(f"[SheetsManager] get_all_subscriptions error: {exc}")
            return []

    def add_subscription(self, amount: float, currency: str, memo: str) -> tuple[bool, str]:
        """サブスクを登録する。"""
        currency = currency.upper()
        if currency not in Config.SUPPORTED_CURRENCIES:
            currency = Config.DEFAULT_CURRENCY
        try:
            now = _now()
            self._subscriptions_sheet().append_row(
                [amount, currency, memo, now.strftime("%Y-%m-%d")],
                value_input_option="USER_ENTERED",
            )
            return True, ""
        except Exception as exc:
            print(f"[SheetsManager] add_subscription error: {exc}")
            return False, str(exc)

    def delete_subscription(self, amount: float, memo: str, currency: str | None = None) -> bool:
        """金額と備考が一致する最初のサブスクを削除する。見つからなければ False。"""
        try:
            sheet = self._subscriptions_sheet()
            records = sheet.get_all_records()
            for i, row in enumerate(records, start=2):
                if (float(row.get("金額", 0)) == amount
                        and str(row.get("備考", "")).strip() == memo
                        and (currency is None
                             or str(row.get("通貨", "")).strip().upper() == currency)):
                    sheet.delete_rows(i)
                    return True
        except Exception as exc:
            print(f"[SheetsManager] delete_subscription error: {exc}")
        return False

    def _get_subscription_totals(
        self,
    ) -> dict[str, tuple[float, list[tuple[str, float]]]]:
        """サブスク合計を通貨ごとに返す。{currency: (total, [(memo, amount), ...])}"""
        result: dict[str, tuple[float, list[tuple[str, float]]]] = {}
        for row in self.get_all_subscriptions():
            currency = str(row.get("通貨", Config.DEFAULT_CURRENCY)).upper()
            if currency not in Config.SUPPORTED_CURRENCIES:
                currency = Config.DEFAULT_CURRENCY
            amount = float(row.get("金額", 0))
            memo = str(row.get("備考", "")).strip()
            if currency not in result:
                result[currency] = (0.0, [])
            total, items = result[currency]
            total += amount
            items.append((memo, amount))
            result[currency] = (total, items)
        return result

    def _filter_by_dates(
        self, all_records: list[dict], start_date: str, end_date: str
    ) -> list[dict]:
        """事前取得済みレコードを日付範囲でフィルタする。"""
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end   = datetime.strptime(end_date,   "%Y-%m-%d").date()
        return [
            r for r in all_records
            if (d := _parse_date(str(r.get("日付", "")))) is not None and start <= d <= end
        ]

    # ------------------------------------------------------------------
    # 集計ヘルパー
    # ------------------------------------------------------------------

    def _aggregate(
        self, records: list[dict]
    ) -> dict[str, tuple[float, dict[str, float]]]:
        """レコードを通貨ごとに集計する。
        戻り値: {currency: (total, {category: amount})}
        """
        result: dict[str, tuple[float, dict[str, float]]] = {}
        for r in records:
            currency = str(r.get("通貨", Config.DEFAULT_CURRENCY)).upper()
            if currency not in Config.SUPPORTED_CURRENCIES:
                currency = Config.DEFAULT_CURRENCY
            amt = float(r.get("金額", 0))
            cat = str(r.get("カテゴリ", "その他"))

            total, by_cat = result.get(currency, (0.0, {}))
            total += amt
            by_cat[cat] = by_cat.get(cat, 0.0) + amt
            result[currency] = (total, by_cat)
        return result

    def _build_budget_lines(
        self,
        currency: str,
        budgets: dict[str, tuple[float, str]],
        report_period: str,
        agg_day:   dict[str, tuple[float, dict[str, float]]],
        agg_week:  dict[str, tuple[float, dict[str, float]]],
        agg_month: dict[str, tuple[float, dict[str, float]]],
        days_elapsed: int = 1,
        week_days_elapsed: int = 7,
    ) -> list[str]:
        """予算比較行を生成する。
        report_period: '日'|'週'|'月' (どのレポートから呼ばれているか)
        各予算の period に応じて適切な集計データと比較する。
        """
        lines: list[str] = []
        for cat, (budget_amt, budget_period) in sorted(budgets.items()):
            if budget_amt <= 0:
                continue

            # 予算の期間に応じて比較する集計と予算額を決める
            if budget_period == "日":
                effective_budget = budget_amt * (
                    days_elapsed if report_period == "月" else
                    week_days_elapsed if report_period == "週" else 1
                )
                agg = agg_day if report_period == "日" else (
                    agg_week if report_period == "週" else agg_month
                )
            elif budget_period == "週":
                effective_budget = budget_amt
                agg = agg_week
            else:  # 月
                effective_budget = budget_amt
                agg = agg_month

            spent = agg.get(currency, (0.0, {}))[1].get(cat, 0.0)
            diff  = effective_budget - spent
            sign  = "✅" if diff >= 0 else "⚠️"
            word  = "節約" if diff >= 0 else "オーバー"
            lines.append(f"{sign} {cat}：{fmt(abs(diff), currency)} {word}")
        return lines

    # ------------------------------------------------------------------
    # レポート
    # ------------------------------------------------------------------

    def _fetch_period_aggregates(self) -> tuple[
        dict, dict, dict, int, int, str, str, str, str
    ]:
        """日・週・月の集計を一括取得する。レポート共通の前処理。
        戻り値: (agg_day, agg_week, agg_month, days_elapsed, week_days_elapsed,
                 today_str, week_start_str, month_start_str, month_end_str)
        """
        now       = _now()
        today     = now.date()
        today_str = today.strftime("%Y-%m-%d")

        week_start     = now - timedelta(days=now.weekday())
        week_start_str = week_start.strftime("%Y-%m-%d")
        week_days_elapsed = now.weekday() + 1  # 月=1〜日=7

        payday = self.get_payday()
        month_start, month_end = _get_pay_period(payday, today)
        month_start_str = month_start.strftime("%Y-%m-%d")
        month_end_str   = month_end.strftime("%Y-%m-%d")
        days_elapsed    = (today - month_start).days + 1

        try:
            all_records = self._expenses_sheet().get_all_records()
        except Exception as exc:
            print(f"[SheetsManager] _fetch_period_aggregates error: {exc}")
            all_records = []

        agg_day   = self._aggregate(self._filter_by_dates(all_records, today_str, today_str))
        agg_week  = self._aggregate(self._filter_by_dates(all_records, week_start_str, today_str))
        agg_month = self._aggregate(self._filter_by_dates(all_records, month_start_str, today_str))

        return (agg_day, agg_week, agg_month,
                days_elapsed, week_days_elapsed,
                today_str, week_start_str, month_start_str, month_end_str)

    def get_daily_report(self) -> str:
        now    = _now()
        today  = now.date()
        (agg_day, agg_week, agg_month,
         days_elapsed, week_days_elapsed,
         today_str, week_start_str, month_start_str, _) = self._fetch_period_aggregates()

        budgets = self.get_all_budgets()
        lines   = ["📊 **本日の支出**"]

        first_block = True
        for currency in Config.SUPPORTED_CURRENCIES:
            if currency not in agg_day:
                continue
            if not first_block:
                lines.append("")
            first_block = False
            total, by_cat = agg_day[currency]
            for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"{cat}：{fmt(amt, currency)}")
            lines.append("──────────────")
            lines.append(f"合計：{fmt(total, currency)}")
            budget_lines = self._build_budget_lines(
                currency, budgets, "日",
                agg_day, agg_week, agg_month,
                days_elapsed, week_days_elapsed,
            )
            if budget_lines:
                lines.append("")
                lines.extend(budget_lines)

        if not agg_day:
            lines.append("本日の支出はありません。")

        return "\n".join(lines)

    def get_weekly_report(self) -> str:
        now = _now()
        week_start = now - timedelta(days=now.weekday())
        week_end   = week_start + timedelta(days=6)

        (agg_day, agg_week, agg_month,
         days_elapsed, week_days_elapsed,
         today_str, week_start_str, month_start_str, _) = self._fetch_period_aggregates()

        budgets = self.get_all_budgets()
        lines   = [
            "📅 **今週の支出**",
            f"期間: {week_start.strftime('%m/%d')} 〜 {week_end.strftime('%m/%d')}",
        ]

        first_block = True
        for currency in Config.SUPPORTED_CURRENCIES:
            if currency not in agg_week:
                continue
            if not first_block:
                lines.append("")
            first_block = False
            total, by_cat = agg_week[currency]
            for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"{cat}：{fmt(amt, currency)}")
            lines.append("──────────────")
            lines.append(f"合計：{fmt(total, currency)}")
            budget_lines = self._build_budget_lines(
                currency, budgets, "週",
                agg_day, agg_week, agg_month,
                days_elapsed, week_days_elapsed,
            )
            if budget_lines:
                lines.append("")
                lines.extend(budget_lines)

        if not agg_week:
            lines.append("今週の支出はありません。")

        return "\n".join(lines)

    def get_current_period_report(self) -> str:
        """現在の給与期間（今月）の進行中レポートを返す。"""
        now   = _now()
        today = now.date()
        payday = self.get_payday()
        start, end = _get_pay_period(payday, today)
        days_total   = (end - start).days + 1
        days_elapsed = (today - start).days + 1

        (agg_day, agg_week, agg_month,
         _, week_days_elapsed,
         __, ___, ____, _____) = self._fetch_period_aggregates()

        budgets = self.get_all_budgets()
        try:
            income_records = self._income_sheet().get_all_records()
        except Exception as exc:
            print(f"[SheetsManager] get_current_period_report income fetch error: {exc}")
            income_records = []

        period_ym = start.strftime("%Y-%m")
        sub_totals = self._get_subscription_totals()
        lines = [
            "📊 **今月の支出**",
            f"期間: {start.strftime('%m/%d')} 〜 {end.strftime('%m/%d')}　({days_elapsed}/{days_total}日経過)",
        ]

        first_block = True
        for currency in Config.SUPPORTED_CURRENCIES:
            expense_total, by_cat = agg_month.get(currency, (0.0, {}))
            income = _income_from_records(income_records, currency, period_ym)
            sub_total, sub_items = sub_totals.get(currency, (0.0, []))
            if expense_total == 0.0 and income == 0.0 and sub_total == 0.0:
                continue

            if not first_block:
                lines.append("")
            first_block = False
            if income > 0:
                lines.append(f"収入：{fmt(income, currency)}")
            for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"{cat}：{fmt(amt, currency)}")
            if sub_items:
                lines.append("📱 サブスク")
                for memo, amt in sub_items:
                    lines.append(f"　{memo}：{fmt(amt, currency)}")
            lines.append("──────────────")
            total_with_subs = expense_total + sub_total
            lines.append(f"合計：{fmt(total_with_subs, currency)}")
            budget_lines = self._build_budget_lines(
                currency, budgets, "月",
                agg_day, agg_week, agg_month,
                days_elapsed, week_days_elapsed,
            )
            if budget_lines:
                lines.append("")
                lines.extend(budget_lines)
            if income > 0:
                remaining = income - total_with_subs
                lines.append("")
                if remaining >= 0:
                    lines.append(f"💰 残り使える額：**{fmt(remaining, currency)}**")
                else:
                    lines.append(f"⚠️ 収入オーバー：**{fmt(abs(remaining), currency)}**")

        if len(lines) == 2:
            lines.append("支出はありません。")

        return "\n".join(lines)

    def get_monthly_report(self) -> str:
        """直前の給与期間の確定レポートを返す（給料日の定期配信用）。"""
        today  = _now().date()
        payday = self.get_payday()
        start, end = _get_pay_period(payday, today - timedelta(days=1))
        days_total = (end - start).days + 1
        target_ym  = start.strftime("%Y-%m")

        all_records = []
        try:
            all_records = self._expenses_sheet().get_all_records()
        except Exception as exc:
            print(f"[SheetsManager] get_monthly_report fetch error: {exc}")

        agg_month = self._aggregate(
            self._filter_by_dates(all_records, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        )
        # 週集計は月末の週とする（budget_lines の週予算比較用）
        week_start = end - timedelta(days=end.weekday())
        agg_week = self._aggregate(
            self._filter_by_dates(all_records, week_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        )
        agg_day = self._aggregate(
            self._filter_by_dates(all_records, end.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        )

        budgets = self.get_all_budgets()
        try:
            income_records = self._income_sheet().get_all_records()
        except Exception as exc:
            print(f"[SheetsManager] get_monthly_report income fetch error: {exc}")
            income_records = []

        sub_totals = self._get_subscription_totals()
        lines = [f"📆 **月次レポート ({start.strftime('%m/%d')} 〜 {end.strftime('%m/%d')})**"]

        first_block = True
        for currency in Config.SUPPORTED_CURRENCIES:
            expense_total, by_cat = agg_month.get(currency, (0.0, {}))
            income = _income_from_records(income_records, currency, target_ym)
            sub_total, sub_items = sub_totals.get(currency, (0.0, []))
            if expense_total == 0.0 and income == 0.0 and sub_total == 0.0:
                continue

            if not first_block:
                lines.append("")
            first_block = False
            if income > 0:
                lines.append(f"収入：{fmt(income, currency)}")
            for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"{cat}：{fmt(amt, currency)}")
            if sub_items:
                lines.append("📱 サブスク")
                for memo, amt in sub_items:
                    lines.append(f"　{memo}：{fmt(amt, currency)}")
            lines.append("──────────────")
            total_with_subs = expense_total + sub_total
            lines.append(f"合計：{fmt(total_with_subs, currency)}")
            budget_lines = self._build_budget_lines(
                currency, budgets, "月",
                agg_day, agg_week, agg_month,
                days_total, 7,
            )
            if budget_lines:
                lines.append("")
                lines.extend(budget_lines)
            if income > 0:
                lines.append("")
                savings = income - total_with_subs
                if savings >= 0:
                    lines.append(f"💰 今月の貯金：**{fmt(savings, currency)}**")
                else:
                    lines.append(f"⚠️ 収入オーバー：**{fmt(abs(savings), currency)}**")

        if len(lines) == 1:
            lines.append("先月の支出・収入はありません。")

        return "\n".join(lines)
