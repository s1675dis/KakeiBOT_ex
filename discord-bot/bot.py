"""
KakeiBOT - 家計管理 Discord Bot

メッセージ形式: <金額> <カテゴリ> [通貨]
例: 560 家賃          → JPY (省略時デフォルト)
    10.26 食費 USD    → USD
    1200 光熱費 JPY   → JPY (明示)

コマンド一覧は !help で確認できます。
"""

import re
import subprocess
import sys
from datetime import datetime, time

import discord
from datetime import timezone, timedelta

import pytz
from discord.ext import commands, tasks

from config import Config
from setup_spreadsheet import setup as spreadsheet_setup, validate as spreadsheet_validate
from sheets_manager import SheetsManager, _get_pay_period, fmt

# 末尾の通貨コード (JPY/USD) は省略可能。省略時は DEFAULT_CURRENCY が使われる。
_CURRENCIES = "|".join(Config.SUPPORTED_CURRENCIES)
# カテゴリあり: "560 家賃" / "-10.26 食費 USD" (マイナスは取消)
EXPENSE_PATTERN = re.compile(
    rf"^(-?\d+(?:\.\d+)?)\s+(.+?)(?:\s+({_CURRENCIES}))?$", re.IGNORECASE
)
# 数字のみ (カテゴリ省略): "560" / "-560 USD" → 食費として扱う
AMOUNT_ONLY_PATTERN = re.compile(
    rf"^(-?\d+(?:\.\d+)?)(?:\s+({_CURRENCIES}))?$", re.IGNORECASE
)
INCOME_PATTERN = re.compile(
    rf"^!収入\s+(\d+(?:\.\d+)?)(?:\s+({_CURRENCIES}))?$", re.IGNORECASE
)
DEFAULT_CATEGORY = "食費"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
sheets = SheetsManager()
jst = pytz.timezone(Config.TIMEZONE)
_JST_FIXED = timezone(timedelta(hours=9))  # tasks.loop の time() 用 (pytz は LMT オフセットになるため)


# ------------------------------------------------------------------
# ヘルパー
# ------------------------------------------------------------------


async def _reply_thread(ctx: commands.Context) -> discord.abc.Messageable:
    """コマンドの返信先を返す。
    スレッド外: コマンドメッセージにスレッドを作成して返す。
    スレッド内: そのスレッドをそのまま返す。
    スレッド作成に失敗した場合はチャンネルにフォールバック。
    """
    if isinstance(ctx.channel, discord.Thread):
        return ctx.channel
    try:
        return await ctx.message.create_thread(name=ctx.invoked_with[:100])
    except discord.HTTPException:
        return ctx.channel


# ------------------------------------------------------------------
# イベント
# ------------------------------------------------------------------


@bot.event
async def on_ready() -> None:
    print(f"[Bot] ログイン: {bot.user} (id={bot.user.id})")
    print("[Bot] スプレッドシートを検証中...")

    ok, issues = spreadsheet_validate()
    if ok:
        print("[Bot] ✅ スプレッドシートの検証OK")
    else:
        print("[Bot] ⚠️ 以下の問題が見つかりました:")
        for issue in issues:
            print(f"       - {issue}")
        print("[Bot] 🔧 自動セットアップを実行します...")
        try:
            spreadsheet_setup()
            print("[Bot] ✅ セットアップ完了")
        except Exception as exc:
            print(f"[Bot] ❌ セットアップ失敗: {exc}")
            print("[Bot] ⚠️  手動で setup_spreadsheet.py を実行してください")

    daily_report.start()
    weekly_report.start()
    monthly_report.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot and not message.webhook_id:
        return
    if message.channel.id != Config.EXPENSE_CHANNEL_ID:
        await bot.process_commands(message)
        return

    content = message.content.strip()
    default_currency = sheets.get_default_currency()

    income_match = INCOME_PATTERN.match(content)
    if income_match:
        amount   = float(income_match.group(1))
        currency = (income_match.group(2) or default_currency).upper()
        success, error_msg = sheets.set_income(amount, currency)
        if success:
            await message.add_reaction("✅")
        else:
            await message.add_reaction("❌")
            await message.channel.send(f"⚠️ 収入の登録に失敗しました。\n```{error_msg}```")
        return

    match = EXPENSE_PATTERN.match(content)
    if match:
        amount = float(match.group(1))
        category = match.group(2).strip()
        currency = (match.group(3) or default_currency).upper()
    else:
        match = AMOUNT_ONLY_PATTERN.match(content)
        if match:
            amount = float(match.group(1))
            category = DEFAULT_CATEGORY
            currency = (match.group(2) or default_currency).upper()
        else:
            await bot.process_commands(message)
            return

    if amount < 0:
        success = sheets.delete_expense(abs(amount), category, currency)
        if success:
            await message.add_reaction("🗑️")
        else:
            await message.add_reaction("❌")
            await message.channel.send(
                f"⚠️ 該当する支出記録が見つかりませんでした。({fmt(abs(amount), currency)} / {category})",
                delete_after=10,
            )
    else:
        success = sheets.add_expense(amount, category, currency)
        if success:
            await message.add_reaction("✅")
        else:
            await message.add_reaction("❌")
            await message.channel.send("⚠️ スプレッドシートへの記録に失敗しました。")


# ------------------------------------------------------------------
# 手動コマンド
# ------------------------------------------------------------------


@bot.command(name="今日")
async def cmd_today(ctx: commands.Context) -> None:
    """本日の支出レポートを表示する。"""
    ch = await _reply_thread(ctx)
    await ch.send(sheets.get_daily_report())


@bot.command(name="今週")
async def cmd_week(ctx: commands.Context) -> None:
    """今週の支出レポートを表示する。"""
    ch = await _reply_thread(ctx)
    await ch.send(sheets.get_weekly_report())


@bot.command(name="今月")
async def cmd_month(ctx: commands.Context) -> None:
    """今月（現在の給与期間）の進行中レポートを表示する。"""
    ch = await _reply_thread(ctx)
    await ch.send(sheets.get_current_period_report())


@bot.command(name="予算")
async def cmd_budget(ctx: commands.Context, *args: str) -> None:
    """予算を表示・設定・削除する。
    !予算                          → 全予算を表示
    !予算 <カテゴリ> <金額> <期間>  → 予算を設定 (期間: 日|週|月)
    !予算 削除 <カテゴリ>           → 予算を削除
    """
    ch = await _reply_thread(ctx)
    currency = sheets.get_default_currency()

    if not args:
        budgets = sheets.get_all_budgets()
        if not budgets:
            await ch.send(
                "📋 予算は未設定です。\n"
                "`!予算 <カテゴリ> <金額> <期間>` で設定できます。\n"
                "例: `!予算 食費 1000 日` / `!予算 家賃 80000 月`"
            )
            return
        lines = ["📋 **設定中の予算**"]
        for cat, (amt, period) in sorted(budgets.items()):
            lines.append(f"　{cat}: {fmt(amt, currency)} / {period}")
        await ch.send("\n".join(lines))
        return

    if args[0] == "削除":
        if len(args) < 2:
            await ch.send("⚠️ カテゴリを指定してください。例: `!予算 削除 食費`")
            return
        category = args[1]
        success = sheets.delete_budget(category)
        if success:
            await ch.send(f"🗑️ **{category}** の予算を削除しました。")
        else:
            await ch.send(f"⚠️ **{category}** の予算が見つかりませんでした。")
        return

    if len(args) < 3:
        await ch.send(
            "⚠️ 引数が不足しています。\n"
            "例: `!予算 食費 1000 日` / `!予算 家賃 80000 月`"
        )
        return

    category, amount_str, period = args[0], args[1], args[2]
    if period not in SheetsManager.VALID_PERIODS:
        await ch.send("⚠️ 期間は `日` `週` `月` のいずれかで指定してください。")
        return
    value = float_or_none(amount_str)
    if value is None or value <= 0:
        await ch.send("⚠️ 正しい金額を入力してください。例: `!予算 食費 1000 日`")
        return

    success, error_msg = sheets.set_budget(category, value, period)
    if success:
        await ch.send(
            f"✅ **{category}** の予算を **{fmt(value, currency)} / {period}** に設定しました。"
        )
    else:
        await ch.send(f"❌ 設定に失敗しました。\n```{error_msg}```")


@bot.command(name="給料日")
async def cmd_payday(ctx: commands.Context, day: str = "") -> None:
    """給料日を表示・設定する。
    使い方: !給料日 → 現在の設定を表示
            !給料日 15 → 毎月15日に設定
    """
    ch = await _reply_thread(ctx)

    if not day:
        current = sheets.get_payday()
        start, end = _get_pay_period(current, datetime.now(jst).date())
        await ch.send(
            f"📅 給料日: 毎月 **{current}日**\n"
            f"現在の期間: {start.strftime('%m/%d')} 〜 {end.strftime('%m/%d')}"
        )
        return

    try:
        d = int(day)
    except ValueError:
        await ch.send("⚠️ 日付は数字で入力してください。例: `!給料日 15`")
        return

    if not 1 <= d <= 31:
        await ch.send("⚠️ 1〜31の範囲で入力してください。")
        return

    success, error_msg = sheets.set_payday(d)
    if success:
        start, end = _get_pay_period(d, datetime.now(jst).date())
        await ch.send(
            f"✅ 給料日を **毎月{d}日** に設定しました。\n"
            f"現在の期間: {start.strftime('%m/%d')} 〜 {end.strftime('%m/%d')}"
        )
    else:
        await ch.send(f"❌ 設定に失敗しました。\n```{error_msg}```")


@bot.command(name="通貨")
async def cmd_currency(ctx: commands.Context, currency: str = "") -> None:
    """通貨を表示・変更する。引数なしで現在の設定を表示、通貨コードを渡すと変更。"""
    ch = await _reply_thread(ctx)
    currencies = ", ".join(Config.SUPPORTED_CURRENCIES)
    current = sheets.get_default_currency()

    if not currency:
        lines = [
            f"💱 **現在のデフォルト通貨**: {current}",
            f"利用可能: {currencies}",
            "",
            f"`!通貨 JPY` または `!通貨 USD` で変更できます。",
        ]
        await ch.send("\n".join(lines))
        return

    currency = currency.upper()
    if currency not in Config.SUPPORTED_CURRENCIES:
        await ch.send(f"⚠️ `{currency}` は未対応です。使用可能: {currencies}")
        return

    success, error_msg = sheets.set_default_currency(currency)
    if success:
        await ch.send(f"✅ デフォルト通貨を **{currency}** に変更しました。")
    else:
        await ch.send(f"❌ 変更に失敗しました。\n```{error_msg}```")


@bot.command(name="収入")
async def cmd_income(ctx: commands.Context, amount: str = "", currency: str = "") -> None:
    """今月の収入を表示・登録する。引数なしで表示、金額を渡すと登録。"""
    ch = await _reply_thread(ctx)

    if not amount:
        now = datetime.now(jst)
        ym = now.strftime("%Y-%m")
        lines = [f"💴 **{now.strftime('%Y年%m月')}の収入**"]
        found = False
        for cur in Config.SUPPORTED_CURRENCIES:
            income = sheets.get_income(cur, ym)
            if income > 0:
                lines.append(f"　{cur}: {fmt(income, cur)}")
                found = True
        if not found:
            lines.append("　未登録です。`!収入 <金額>` で登録してください。")
        await ch.send("\n".join(lines))
        return

    value = float_or_none(amount)
    if value is None or value <= 0:
        await ch.send("⚠️ 正しい金額を入力してください。例: `!収入 1960 USD`")
        return

    cur = (currency.upper() if currency else Config.DEFAULT_CURRENCY)
    if cur not in Config.SUPPORTED_CURRENCIES:
        await ch.send(f"⚠️ 未対応の通貨です。使用可能: {', '.join(Config.SUPPORTED_CURRENCIES)}")
        return

    success, error_msg = sheets.set_income(value, cur)
    if success:
        ym = datetime.now(jst).strftime("%Y年%m月")
        await ch.send(f"✅ {ym}の収入を登録しました: **{fmt(value, cur)}**")
    else:
        await ch.send(f"❌ 収入の登録に失敗しました。\n```{error_msg}```")


@bot.command(name="help", aliases=["ヘルプ"])
async def cmd_help(ctx: commands.Context) -> None:
    """コマンド一覧を表示する。"""
    ch = await _reply_thread(ctx)
    cur = Config.DEFAULT_CURRENCY
    lines = [
        "📖 **コマンド一覧**",
        "",
        "**📥 支出入力** (支出チャンネルのみ)",
        "```",
        f"<金額> <カテゴリ> [通貨]   例: 10.26 食費 {cur}",
        f"<金額> [通貨]              例: 5.00 {cur}  (カテゴリ省略 → 食費)",
        "```",
        "**📊 レポート**",
        "```",
        "!今日          本日の支出",
        "!今週          今週の支出 (月〜日)",
        "!今月          今月の支出 (給与期間)",
        "```",
        "**⚙️ 設定・確認**",
        "```",
        "!予算                          全予算を表示",
        "!予算 <カテゴリ> <金額> <期間>  予算を設定 (期間: 日|週|月)",
        "!予算 削除 <カテゴリ>           予算を削除",
        "                               例: !予算 食費 1000 日",
        "                               例: !予算 家賃 80000 月",
        "!収入                  今月の収入を表示",
        f"!収入 <金額> [通貨]    今月の収入を登録   例: !収入 1960 {cur}",
        "!給料日                給料日を表示",
        "!給料日 <日>           給料日を設定       例: !給料日 15",
        "!通貨                  使用可能な通貨を確認",
        "!サブスク                          サブスク一覧を表示",
        f"!サブスク <金額> <備考> [通貨]     サブスクを登録     例: !サブスク 980 Spotify",
        "!サブスク解約 <金額> <備考>        サブスクを解約     例: !サブスク解約 980 Spotify",
        "!報告                    定時報告のON/OFF状態を表示",
        "!報告 今日 on/off        毎日: 今日の支出",
        "!報告 今週毎日 on/off    毎日: 今週の支出",
        "!報告 今月毎日 on/off    毎日: 今月の支出",
        "!報告 今週 on/off        毎週日曜: 今週レポート",
        "!報告 今月 on/off        毎月給料日: 今月レポート",
        "```",
        "**🕐 定時報告の送信時刻** (変更は .env を編集して !update で再起動)",
        "```",
        f"毎日レポート  : {Config.DAILY_REPORT_HOUR:02d}:{Config.DAILY_REPORT_MINUTE:02d} JST",
        f"  → DAILY_REPORT_HOUR / DAILY_REPORT_MINUTE",
        f"毎週レポート  : 日曜 {Config.WEEKLY_REPORT_HOUR:02d}:{Config.WEEKLY_REPORT_MINUTE:02d} JST",
        f"  → WEEKLY_REPORT_HOUR / WEEKLY_REPORT_MINUTE",
        f"毎月レポート  : 給料日 {Config.MONTHLY_REPORT_HOUR:02d}:{Config.MONTHLY_REPORT_MINUTE:02d} JST",
        f"  → MONTHLY_REPORT_HOUR / MONTHLY_REPORT_MINUTE",
        "```",
    ]
    await ch.send("\n".join(lines))


@bot.command(name="報告")
async def cmd_report_toggle(ctx: commands.Context, *args: str) -> None:
    """定時報告のON/OFFを設定する。
    !報告                  → 現在の設定を表示
    !報告 今日 on/off      → 毎日: 今日の支出をON/OFF
    !報告 今週毎日 on/off  → 毎日: 今週の支出をON/OFF
    !報告 今月毎日 on/off  → 毎日: 今月の支出をON/OFF
    !報告 今週 on/off      → 毎週日曜: 今週レポートをON/OFF
    !報告 今月 on/off      → 毎月給料日: 今月レポートをON/OFF
    """
    ch = await _reply_thread(ctx)
    # label → (スプレッドシートキー, 表示説明)
    _label_map: dict[str, tuple[str, str]] = {
        "今日":     ("日次",      "毎日 今日の支出"),
        "今週毎日": ("日次_今週", "毎日 今週の支出"),
        "今月毎日": ("日次_今月", "毎日 今月の支出"),
        "今週":     ("週次",      "毎週日曜 今週レポート"),
        "今月":     ("月次",      "毎月給料日 今月レポート"),
    }

    if not args:
        lines = ["📢 **定時報告の設定**"]
        for label, (rtype, desc) in _label_map.items():
            status = "✅ ON" if sheets.get_report_enabled(rtype) else "❌ OFF"
            lines.append(f"　{label} ({desc}): {status}")
        lines.append("")
        lines.append("`!報告 今日 off` のように変更できます。")
        await ch.send("\n".join(lines))
        return

    if len(args) < 2:
        await ch.send("⚠️ 使い方: `!報告 今日 on` / `!報告 今週毎日 off`")
        return

    label, state_str = args[0], args[1].lower()
    if label not in _label_map:
        keys = " ".join(f"`{k}`" for k in _label_map)
        await ch.send(f"⚠️ `{label}` は無効です。{keys} のいずれかを指定してください。")
        return
    if state_str not in ("on", "off"):
        await ch.send("⚠️ `on` または `off` を指定してください。")
        return

    rtype, desc = _label_map[label]
    enabled = state_str == "on"
    success, error_msg = sheets.set_report_enabled(rtype, enabled)
    if success:
        status = "✅ ON" if enabled else "❌ OFF"
        await ch.send(f"📢 {desc}の定時報告を **{status}** にしました。")
    else:
        await ch.send(f"❌ 設定に失敗しました。\n```{error_msg}```")


@bot.command(name="サブスク")
async def cmd_subscription(ctx: commands.Context, *args: str) -> None:
    """サブスクを表示・登録する。
    !サブスク                        → 一覧表示
    !サブスク <金額> <備考> [通貨]   → 登録
    """
    ch = await _reply_thread(ctx)
    default_currency = sheets.get_default_currency()

    if not args:
        subs = sheets.get_all_subscriptions()
        if not subs:
            await ch.send(
                "📱 登録済みのサブスクはありません。\n"
                "`!サブスク <金額> <備考>` で登録できます。"
            )
            return
        lines = ["📱 **登録済みサブスク**"]
        totals: dict[str, float] = {}
        for row in subs:
            cur = str(row.get("通貨", default_currency)).upper()
            amt = float(row.get("金額", 0))
            memo = str(row.get("備考", ""))
            lines.append(f"　{memo}：{fmt(amt, cur)}")
            totals[cur] = totals.get(cur, 0.0) + amt
        lines.append("──────────────")
        for cur, total in totals.items():
            lines.append(f"合計：{fmt(total, cur)} / 月")
        lines.append("\n`!サブスク解約 <金額> <備考>` で解約できます。")
        await ch.send("\n".join(lines))
        return

    value = float_or_none(args[0])
    if value is None or value <= 0:
        await ch.send("⚠️ 正しい金額を入力してください。例: `!サブスク 980 Spotify`")
        return
    if len(args) < 2:
        await ch.send("⚠️ 備考を入力してください。例: `!サブスク 980 Spotify`")
        return

    if len(args) >= 3 and args[-1].upper() in Config.SUPPORTED_CURRENCIES:
        currency = args[-1].upper()
        memo = " ".join(args[1:-1])
    else:
        currency = default_currency
        memo = " ".join(args[1:])

    success, error_msg = sheets.add_subscription(value, currency, memo)
    if success:
        await ch.send(f"✅ **{memo}** ({fmt(value, currency)} / 月) を登録しました。")
    else:
        await ch.send(f"❌ 登録に失敗しました。\n```{error_msg}```")


@bot.command(name="サブスク解約")
async def cmd_subscription_cancel(ctx: commands.Context, *args: str) -> None:
    """サブスクを解約する。
    !サブスク解約 <金額> <備考> [通貨]
    """
    ch = await _reply_thread(ctx)

    if len(args) < 2:
        await ch.send("⚠️ 使い方: `!サブスク解約 <金額> <備考>`\n例: `!サブスク解約 980 Spotify`")
        return

    value = float_or_none(args[0])
    if value is None or value <= 0:
        await ch.send("⚠️ 正しい金額を入力してください。例: `!サブスク解約 980 Spotify`")
        return

    if args[-1].upper() in Config.SUPPORTED_CURRENCIES:
        currency: str | None = args[-1].upper()
        memo = " ".join(args[1:-1])
    else:
        currency = None
        memo = " ".join(args[1:])

    success = sheets.delete_subscription(value, memo, currency)
    if success:
        await ch.send(f"✅ **{memo}** のサブスクを解約しました。")
    else:
        await ch.send(f"⚠️ **{memo}** のサブスクが見つかりませんでした。")


@bot.command(name="update")
async def cmd_update(ctx: commands.Context) -> None:
    """GitHubから最新コードを取得してBotを再起動する。"""
    if ctx.author.id != Config.OWNER_ID:
        await ctx.send("⚠️ このコマンドは管理者のみ使用できます。")
        return

    ch = await _reply_thread(ctx)
    await ch.send("🔄 アップデートを開始します...")
    try:
        result = subprocess.run(
            ["git", "pull", "origin", "main"],
            capture_output=True,
            text=True,
            cwd="/home/s1675dis/KakeiBOT_ex",
        )
        output = result.stdout.strip() or result.stderr.strip() or "(出力なし)"
        output = output[:1900]  # Discord の 2000 文字制限に収める
        await ch.send(f"```{output}```")
    except Exception as e:
        await ch.send(f"❌ git pull 失敗: {e}")
        return

    await ch.send("♻️ 再起動します...")
    sys.exit(0)


def float_or_none(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


# ------------------------------------------------------------------
# 定期タスク
# ------------------------------------------------------------------


@tasks.loop(
    time=time(
        hour=Config.DAILY_REPORT_HOUR,
        minute=Config.DAILY_REPORT_MINUTE,
        tzinfo=_JST_FIXED,
    )
)
async def daily_report() -> None:
    """毎日設定時刻に日次・今週・今月レポートを送信する（各ON/OFF設定に従う）。"""
    channel = bot.get_channel(Config.REPORT_CHANNEL_ID)
    if not channel:
        return
    if sheets.get_report_enabled("日次"):
        await channel.send(sheets.get_daily_report())
    if sheets.get_report_enabled("日次_今週"):
        await channel.send(sheets.get_weekly_report())
    if sheets.get_report_enabled("日次_今月"):
        await channel.send(sheets.get_current_period_report())


@tasks.loop(
    time=time(
        hour=Config.WEEKLY_REPORT_HOUR,
        minute=Config.WEEKLY_REPORT_MINUTE,
        tzinfo=_JST_FIXED,
    )
)
async def weekly_report() -> None:
    """毎週日曜日に週次レポートを送信する。"""
    if not sheets.get_report_enabled("週次"):
        return
    if datetime.now(jst).weekday() != 6:  # 6 = Sunday
        return
    channel = bot.get_channel(Config.REPORT_CHANNEL_ID)
    if channel:
        await channel.send(sheets.get_weekly_report())


@tasks.loop(
    time=time(
        hour=Config.MONTHLY_REPORT_HOUR,
        minute=Config.MONTHLY_REPORT_MINUTE,
        tzinfo=_JST_FIXED,
    )
)
async def monthly_report() -> None:
    """給料日に前期間の月次レポートを送信する。"""
    if not sheets.get_report_enabled("月次"):
        return
    if datetime.now(jst).day != sheets.get_payday():
        return
    channel = bot.get_channel(Config.REPORT_CHANNEL_ID)
    if channel:
        await channel.send(sheets.get_monthly_report())


if __name__ == "__main__":
    bot.run(Config.DISCORD_TOKEN)
