"""
Google Spreadsheetの初期セットアップスクリプト

実行すると以下のシートとヘッダーを自動で作成します：
- 支出記録: 日付 / 時刻 / カテゴリ / 金額 / 通貨
- 予算設定: 項目 / 金額  (デフォルト1日予算: JPY¥3,000 / USD$30)
- 収入記録: 年月 / 金額 / 通貨
- サブスク: 金額 / 通貨 / 備考 / 登録日

使い方:
    python setup_spreadsheet.py
"""

import gspread
import pytz
from google.oauth2.service_account import Credentials
from config import Config

# 各シートの必須ヘッダー定義
REQUIRED_SHEETS: dict[str, list[str]] = {
    "支出記録": ["日付", "時刻", "カテゴリ", "金額", "通貨"],
    "予算設定": ["項目", "金額"],
    "収入記録": ["年月", "金額", "通貨"],
    "サブスク":  ["金額", "通貨", "備考", "登録日"],
}


def _connect() -> gspread.Spreadsheet:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(
        Config.GOOGLE_CREDENTIALS_FILE, scopes=scopes
    )
    return gspread.authorize(creds).open_by_key(Config.SPREADSHEET_ID)


def validate() -> tuple[bool, list[str]]:
    """スプレッドシートが要件を満たすか検証する。
    戻り値: (OK フラグ, 問題点のリスト)
    """
    issues: list[str] = []
    try:
        spreadsheet = _connect()
        existing = {ws.title: ws for ws in spreadsheet.worksheets()}

        for sheet_name, required_headers in REQUIRED_SHEETS.items():
            if sheet_name not in existing:
                issues.append(f"シート「{sheet_name}」が存在しません")
                continue
            actual_headers = existing[sheet_name].row_values(1)
            for h in required_headers:
                if h not in actual_headers:
                    issues.append(f"「{sheet_name}」にヘッダー「{h}」がありません")

    except Exception as exc:
        issues.append(f"スプレッドシートへの接続に失敗しました: {exc}")

    return (len(issues) == 0, issues)


def setup() -> None:
    spreadsheet = _connect()
    existing_sheets = {ws.title for ws in spreadsheet.worksheets()}

    # --- 支出記録シート ---
    if "支出記録" not in existing_sheets:
        ws = spreadsheet.add_worksheet(title="支出記録", rows=1000, cols=6)
        print("✅ シート「支出記録」を作成しました")
    else:
        ws = spreadsheet.worksheet("支出記録")
        print("ℹ️  シート「支出記録」は既に存在します")

    # ヘッダーが空のときのみ書き込む
    if not ws.row_values(1):
        ws.update("A1:E1", [["日付", "時刻", "カテゴリ", "金額", "通貨"]])
        ws.format("A1:E1", {"textFormat": {"bold": True}})
        print("✅ 支出記録のヘッダーを設定しました (日付/時刻/カテゴリ/金額/通貨)")

    # --- 予算設定シート ---
    if "予算設定" not in existing_sheets:
        ws2 = spreadsheet.add_worksheet(title="予算設定", rows=20, cols=3)
        print("✅ シート「予算設定」を作成しました")
    else:
        ws2 = spreadsheet.worksheet("予算設定")
        print("ℹ️  シート「予算設定」は既に存在します")

    if not ws2.row_values(1):
        ws2.update(
            "A1:B3",
            [
                ["項目", "金額"],
                ["1日の予算_JPY", 3000],
                ["1日の予算_USD", 30],
            ],
        )
        ws2.format("A1:B1", {"textFormat": {"bold": True}})
        print("✅ 予算設定のデフォルト値を設定しました")
        print("   JPY: ¥3,000/日  USD: $30/日")
        print("   ⚠️  予算設定シートの B2・B3 を実際の1日予算に変更してください")

    # --- 収入記録シート ---
    if "収入記録" not in existing_sheets:
        ws3 = spreadsheet.add_worksheet(title="収入記録", rows=200, cols=3)
        print("✅ シート「収入記録」を作成しました")
    else:
        ws3 = spreadsheet.worksheet("収入記録")
        print("ℹ️  シート「収入記録」は既に存在します")

    if not ws3.row_values(1):
        ws3.update("A1:C1", [["年月", "金額", "通貨"]])
        ws3.format("A1:C1", {"textFormat": {"bold": True}})
        print("✅ 収入記録のヘッダーを設定しました (年月/金額/通貨)")

    # --- サブスクシート ---
    if "サブスク" not in existing_sheets:
        ws4 = spreadsheet.add_worksheet(title="サブスク", rows=100, cols=4)
        print("✅ シート「サブスク」を作成しました")
    else:
        ws4 = spreadsheet.worksheet("サブスク")
        print("ℹ️  シート「サブスク」は既に存在します")

    if not ws4.row_values(1):
        ws4.update("A1:D1", [["金額", "通貨", "備考", "登録日"]])
        ws4.format("A1:D1", {"textFormat": {"bold": True}})
        print("✅ サブスクのヘッダーを設定しました (金額/通貨/備考/登録日)")

    print("\n✅ セットアップ完了！")


if __name__ == "__main__":
    setup()
