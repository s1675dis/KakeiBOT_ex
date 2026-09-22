import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from batch_input import parse_batch_expenses
from sheets_manager import SheetsManager
with patch.object(SheetsManager, "__init__", return_value=None):
    import bot


class ParseBatchTests(unittest.TestCase):
    def test_user_example(self):
        entries = parse_batch_expenses('!入力 260917-260921\n1.2\n3.1\n2.19\n7.98\n7.45 娯楽')
        self.assertEqual([entry[0] for entry in entries], [date(2026, 9, 17)] * 5)
        self.assertEqual([entry[1] for entry in entries], [1.2, 3.1, 2.19, 7.98, 7.45])
        self.assertEqual([entry[2] for entry in entries], ['食費'] * 4 + ['娯楽'])

    def test_blank_lines_advance_day(self):
        entries = parse_batch_expenses('!入力 260917-260921\n1.2\n3.1\n\n2.19\n7.98\n\n7.45 娯楽')
        self.assertEqual([entry[0].day for entry in entries], [17, 17, 18, 18, 19])

    def test_crlf_whitespace_and_repeated_blank_lines(self):
        entries = parse_batch_expenses('\r\n!入力 260917-260918\r\n\r\n1.2\r\n  \r\n\r\n7.45 娯楽\r\n\r\n')
        self.assertEqual([entry[0].day for entry in entries], [17, 18])

    def test_month_year_and_leap_boundaries(self):
        for span, expected in [
            ('261231-270101', [date(2026, 12, 31), date(2027, 1, 1)]),
            ('280228-280301', [date(2028, 2, 28), date(2028, 2, 29)]),
            ('260930-261001', [date(2026, 9, 30), date(2026, 10, 1)]),
            ('690101-690102', [date(2069, 1, 1), date(2069, 1, 2)]),
        ]:
            with self.subTest(span=span):
                self.assertEqual([e[0] for e in parse_batch_expenses(f'!入力 {span}\n0\n\n1')], expected)

    def test_invalid_input(self):
        for text in [
            '', '!入力', '!入力 260230-260301\n1',
            '!入力 260921-260917\n1', '!入力 260917-260917\n1\n\n2',
            '!入力 260917-260921\n', '!入力 260917-260921\n1\nnope',
            '!入力 260917-260921\n-1 娯楽', '!入力 260917-260921\nNaN',
            '!入力 260917-260921\n' + '9' * 400,
        ]:
            with self.subTest(text=text[:60]), self.assertRaises(ValueError):
                parse_batch_expenses(text)


class SheetsBatchTests(unittest.TestCase):
    def setUp(self):
        self.manager = SheetsManager.__new__(SheetsManager)
        self.sheet = Mock()
        self.manager._expenses_sheet = Mock(return_value=self.sheet)

    def test_single_append_keeps_dates_numbers_and_literal_category(self):
        entries = parse_batch_expenses('!入力 260917-260918\n1.2\n3.1\n\n7.45 =1+1', 'USD')
        with patch('sheets_manager._now', return_value=datetime(2026, 9, 22, 12, 34, 56)):
            self.assertTrue(self.manager.add_expenses(entries))
        self.sheet.append_rows.assert_called_once()
        rows = self.sheet.append_rows.call_args.args[0]
        self.assertEqual([r[1] for r in rows], ['12:34:56'] * 3)
        self.assertEqual([(r[0], r[2], r[3], r[4]) for r in rows], [
            ('2026-09-17', "'食費", 1.2, 'USD'),
            ('2026-09-17', "'食費", 3.1, 'USD'),
            ('2026-09-18', "'=1+1", 7.45, 'USD'),
        ])
        self.assertEqual(self.sheet.append_rows.call_args.kwargs, {'value_input_option': 'USER_ENTERED'})

    def test_dates_and_times_are_not_escaped_but_categories_are(self):
        categories = ['123', '2026-09-17', '=1+1', "'カテゴリ", '娯楽']
        entries = [(date(2026, 9, 17), 1.2, category, 'USD') for category in categories]
        with patch('sheets_manager._now', return_value=datetime(2026, 9, 22, 0, 0, 0)):
            self.assertTrue(self.manager.add_expenses(entries))
        rows = self.sheet.append_rows.call_args.args[0]
        for row, category in zip(rows, categories):
            self.assertEqual(row[:2], ['2026-09-17', '00:00:00'])
            self.assertEqual(row[2], "'" + category)
            self.assertEqual(row[3:], [1.2, 'USD'])
        self.assertEqual(self.sheet.append_rows.call_args.kwargs,
                         {'value_input_option': 'USER_ENTERED'})

    def test_failure_and_empty_batch(self):
        self.assertFalse(self.manager.add_expenses([]))
        self.sheet.append_rows.assert_not_called()
        self.sheet.append_rows.side_effect = RuntimeError('test failure')
        self.assertFalse(self.manager.add_expenses([(date(2026, 9, 17), 1.2, '食費', 'USD')]))
        self.sheet.append_rows.assert_called_once()


class MessageTests(unittest.IsolatedAsyncioTestCase):
    def message(self, text, webhook=False, channel_id=2):
        return SimpleNamespace(
            content=text, author=SimpleNamespace(bot=webhook), webhook_id=123 if webhook else None,
            channel=SimpleNamespace(id=channel_id, send=AsyncMock()), add_reaction=AsyncMock(),
        )

    async def asyncSetUp(self):
        self.sheets = Mock()
        self.sheets.get_default_currency.return_value = 'USD'
        self.sheet_patch = patch.object(bot, 'sheets', self.sheets)
        self.channel_patch = patch.object(bot.Config, 'EXPENSE_CHANNEL_ID', 2)
        self.sheet_patch.start()
        self.channel_patch.start()
        self.addCleanup(self.sheet_patch.stop)
        self.addCleanup(self.channel_patch.stop)

    async def test_normal_and_webhook_batch(self):
        for webhook in [False, True]:
            self.sheets.reset_mock()
            msg = self.message('!入力 260917-260918\n1.2\n100 JPY\n\n7.45 娯楽 usd', webhook)
            await bot.on_message(msg)
            self.sheets.add_expenses.assert_called_once_with([
                (date(2026, 9, 17), 1.2, '食費', 'USD'),
                (date(2026, 9, 17), 100, '食費', 'JPY'),
                (date(2026, 9, 18), 7.45, '娯楽', 'USD'),
            ])
            msg.add_reaction.assert_awaited_once_with('✅')

    async def test_invalid_batch_writes_nothing(self):
        for content in ['!入力 260917-260918\n1.2\ninvalid', '!入力 260917-260917\n1\n\n2']:
            msg = self.message(content)
            await bot.on_message(msg)
            msg.add_reaction.assert_awaited_once_with('❌')
        self.sheets.add_expenses.assert_not_called()
        self.sheets.add_expense.assert_not_called()

    async def test_existing_single_expense_cancellation_income_and_next_month(self):
        self.sheets.get_next_period_start.return_value = date(2026, 10, 1)
        self.sheets.set_income.return_value = (True, '')
        await bot.on_message(self.message('1.2'))
        self.sheets.add_expense.assert_called_with(1.2, '食費', 'USD', None)
        await bot.on_message(self.message('100 家賃 JPY 来月'))
        self.sheets.add_expense.assert_called_with(100, '家賃', 'JPY', date(2026, 10, 1))
        await bot.on_message(self.message('-7.45 娯楽 USD'))
        self.sheets.delete_expense.assert_called_once_with(7.45, '娯楽', 'USD')
        await bot.on_message(self.message('!収入 1000 USD'))
        self.sheets.set_income.assert_called_once_with(1000, 'USD')
        self.sheets.add_expenses.assert_not_called()

    async def test_other_channel_and_bot_messages_do_not_write(self):
        with patch.object(bot.bot, 'process_commands', new_callable=AsyncMock):
            await bot.on_message(self.message('!入力 260917-260918\n1.2', channel_id=3))
        msg = self.message('!入力 260917-260918\n1.2', webhook=True)
        msg.webhook_id = None
        await bot.on_message(msg)
        self.sheets.add_expenses.assert_not_called()

    async def test_write_failure_does_not_retry(self):
        msg = self.message('!入力 260917-260918\n1.2')
        self.sheets.add_expenses.return_value = False
        await bot.on_message(msg)
        self.sheets.add_expenses.assert_called_once()
        msg.add_reaction.assert_awaited_once_with('❌')
        self.assertIn('再送する前に', msg.channel.send.call_args.args[0])

    async def test_help_fits_discord_message(self):
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(bot, '_reply_thread', new_callable=AsyncMock, return_value=channel):
            await bot.cmd_help.callback(Mock())
        self.assertLessEqual(len(channel.send.call_args.args[0]), 2000)
        self.assertIn('!入力', channel.send.call_args.args[0])


class CurrencyTests(unittest.TestCase):
    def test_currency_and_category(self):
        entries = parse_batch_expenses('!入力 260917-260917\n1 娯楽 USD\n2 jpy\n3 外食 ランチ\n4', 'USD')
        self.assertEqual([(e[2], e[3]) for e in entries], [
            ('娯楽', 'USD'), ('食費', 'JPY'), ('外食 ランチ', 'USD'), ('食費', 'USD'),
        ])


if __name__ == '__main__':
    unittest.main()
