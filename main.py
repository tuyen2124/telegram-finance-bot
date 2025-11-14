# main.py
# Bot Telegram quản lý tài chính cá nhân chuyên nghiệp:
# - Hỗ trợ đơn vị tiền Việt (k, tr, triệu, có . ,)
# - Ghi thu/chi, mục tiêu tiết kiệm, báo cáo
# - Quản lý danh mục, hạn mức chi tiêu, sửa/xoá giao dịch
# - Multi ví: 4 ví mặc định theo quy tắc 4-2-2-2 + lệnh /salary tự chia lương
# - Xuất CSV: /export (toàn bộ), /export_month (theo tháng), /export_wallet (theo ví)
# - Tính năng thêm (không tốn phí): /wallets_add, /transfer, /insights, /backup
# - Webhook với aiogram v3 + aiohttp + SQLite
#
# TẤT CẢ tin nhắn & comment: tiếng Việt.

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, date
from typing import Optional, Tuple

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ==========================
# CẤU HÌNH
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")  # ví dụ: https://your-app.onrender.com
WEBHOOK_PATH = "/telegram-webhook"
WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}" if BASE_WEBHOOK_URL else None
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "finance_bot.db")

# ==========================
# HÀM HỖ TRỢ: PARSE TIỀN VIỆT
# ==========================


def parse_vietnamese_money(text: str) -> float:
    """
    Chuyển các kiểu nhập tiền Việt sang số:
    - 200k, 200K -> 200_000
    - 1tr, 1.5tr, 1,5tr, 1 triệu -> 1_000_000, 1_500_000
    - 150.000, 150,000 -> 150_000
    """
    t = text.lower().strip()

    if not t:
        raise ValueError("Chuỗi trống")

    # bỏ khoảng trắng
    t = t.replace(" ", "")

    # chuẩn hoá 'triệu' -> 'tr'
    if "triệu" in t:
        t = t.replace("triệu", "tr")

    multiplier = 1
    if t.endswith("k"):
        multiplier = 1_000
        t = t[:-1]
    elif t.endswith("tr"):
        multiplier = 1_000_000
        t = t[:-2]

    # bỏ dấu . và , phân tách nghìn/thập phân
    t = t.replace(".", "").replace(",", "")

    if not t:
        raise ValueError("Không tìm thấy số hợp lệ.")

    number = float(t)
    return number * multiplier


def extract_amount_and_note(raw: str) -> Tuple[float, str]:
    """
    Cho phép người dùng nhập kiểu:
      - "35k ăn sáng"
      - "1.2tr tiền nhà"
      - "150000 tiền điện"
    Hàm sẽ:
      - lấy token đầu tiên làm "số tiền"
      - phần còn lại (nếu có) làm ghi chú
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("Chuỗi trống")

    parts = raw.split(maxsplit=1)
    if len(parts) == 1:
        amount = parse_vietnamese_money(parts[0])
        return amount, ""
    else:
        amount_text = parts[0]
        note = parts[1].strip()
        amount = parse_vietnamese_money(amount_text)
        return amount, note


# ==========================
# LỚP DB
# ==========================


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()

        # Người dùng
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                full_name TEXT,
                created_at TEXT
            );
            """
        )

        # Ví (wallets)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                purpose TEXT, -- mô tả mục đích (ví dụ: '4-2-2-2 essential')
                created_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )

        # Giao dịch (có gắn ví)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT, -- 'income' / 'expense'
                amount REAL,
                category TEXT,
                note TEXT,
                wallet_id INTEGER,
                created_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(wallet_id) REFERENCES wallets(id)
            );
            """
        )

        # Mục tiêu tiết kiệm
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS saving_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                target_amount REAL,
                current_amount REAL DEFAULT 0,
                created_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )

        # Lịch sử giao dịch mục tiêu
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS saving_goal_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER,
                type TEXT, -- 'deposit' / 'withdraw'
                amount REAL,
                note TEXT,
                created_at TEXT,
                FOREIGN KEY(goal_id) REFERENCES saving_goals(id)
            );
            """
        )

        # Ghi chú ngân sách 4-2-2-2
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                total_income REAL,
                essential REAL,
                long_term REAL,
                invest REAL,
                personal REAL,
                created_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )

        # Danh mục thu/chi
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                type TEXT -- 'income' / 'expense'
            );
            """
        )

        # Hạn mức chi tiêu
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                category TEXT,
                period TEXT, -- ví dụ 'month'
                limit_amount REAL
            );
            """
        )

        self.conn.commit()

    # --- USER ---

    def get_or_create_user(self, telegram_id: int, full_name: str | None) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        if row:
            user_id = row["id"]
            self.ensure_default_categories(user_id)
            self.ensure_default_wallets(user_id)
            return user_id

        now = datetime.utcnow().isoformat()
        cur.execute(
            "INSERT INTO users (telegram_id, full_name, created_at) VALUES (?, ?, ?)",
            (telegram_id, full_name or "", now),
        )
        self.conn.commit()
        user_id = cur.lastrowid
        self.ensure_default_categories(user_id)
        self.ensure_default_wallets(user_id)
        return user_id

    def get_user_id(self, telegram_id: int) -> Optional[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        return row["id"] if row else None

    # --- CATEGORIES ---

    def ensure_default_categories(self, user_id: int):
        """Tạo một số danh mục mặc định nếu user chưa có danh mục nào."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS c FROM categories WHERE user_id=?",
            (user_id,),
        )
        c = cur.fetchone()["c"]
        if c > 0:
            return
        default_expense = ["Ăn uống", "Đi lại", "Nhà cửa", "Giải trí", "Giáo dục", "Khác"]
        default_income = ["Lương", "Thưởng", "Thu nhập khác"]
        for name in default_expense:
            cur.execute(
                "INSERT INTO categories (user_id, name, type) VALUES (?, ?, 'expense')",
                (user_id, name),
            )
        for name in default_income:
            cur.execute(
                "INSERT INTO categories (user_id, name, type) VALUES (?, ?, 'income')",
                (user_id, name),
            )
        self.conn.commit()

    def get_categories(self, user_id: int, cat_type: Optional[str] = None):
        cur = self.conn.cursor()
        if cat_type:
            cur.execute(
                """
                SELECT id, name, type FROM categories
                WHERE user_id=? AND type=?
                ORDER BY name
                """,
                (user_id, cat_type),
            )
        else:
            cur.execute(
                """
                SELECT id, name, type FROM categories
                WHERE user_id=?
                ORDER BY type, name
                """,
                (user_id,),
            )
        return cur.fetchall()

    def add_category(self, user_id: int, name: str, cat_type: str):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO categories (user_id, name, type) VALUES (?, ?, ?)",
            (user_id, name, cat_type),
        )
        self.conn.commit()

    def delete_category(self, user_id: int, cat_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM categories WHERE id=? AND user_id=?",
            (cat_id, user_id),
        )
        self.conn.commit()

    # --- WALLETS (VÍ) ---

    def ensure_default_wallets(self, user_id: int):
        """Tạo 4 ví mặc định tương ứng 4 khoản 4-2-2-2 nếu user chưa có ví nào."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM wallets WHERE user_id = ?", (user_id,))
        c = cur.fetchone()["c"]
        if c > 0:
            return

        now = datetime.utcnow().isoformat()
        default_wallets = [
            ("Chi tiêu thiết yếu", "4-2-2-2 essential"),
            ("Tiết kiệm dài hạn", "4-2-2-2 long_term"),
            ("Đầu tư & Tự do tài chính", "4-2-2-2 invest"),
            ("Chi tiêu cá nhân & Phát triển", "4-2-2-2 personal"),
        ]
        for name, purpose in default_wallets:
            cur.execute(
                """
                INSERT INTO wallets (user_id, name, purpose, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, name, purpose, now),
            )
        self.conn.commit()

    def get_wallets(self, user_id: int):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT id, name, purpose
            FROM wallets
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        )
        return cur.fetchall()

    def get_wallet(self, user_id: int, wallet_id: int):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT id, name, purpose
            FROM wallets
            WHERE id = ? AND user_id = ?
            """,
            (wallet_id, user_id),
        )
        return cur.fetchone()

    def add_wallet(self, user_id: int, name: str, purpose: str = ""):
        cur = self.conn.cursor()
        now = datetime.utcnow().isoformat()
        cur.execute(
            """
            INSERT INTO wallets (user_id, name, purpose, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, name, purpose, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_wallet_balance(self, user_id: int, wallet_id: int) -> float:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE -amount END), 0) AS bal
            FROM transactions
            WHERE user_id = ? AND wallet_id = ?
            """,
            (user_id, wallet_id),
        )
        row = cur.fetchone()
        return row["bal"] if row else 0.0

    # --- TRANSACTIONS ---

    def add_transaction(
        self,
        user_id: int,
        tx_type: str,
        amount: float,
        category: str,
        note: str,
        wallet_id: Optional[int] = None,
    ):
        cur = self.conn.cursor()
        now = datetime.utcnow().isoformat()
        cur.execute(
            """
            INSERT INTO transactions (user_id, type, amount, category, note, wallet_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, tx_type, amount, category, note, wallet_id, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_balance(self, user_id: int) -> float:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM transactions WHERE user_id=? AND type='income'",
            (user_id,),
        )
        inc = cur.fetchone()["total"]
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM transactions WHERE user_id=? AND type='expense'",
            (user_id,),
        )
        exp = cur.fetchone()["total"]
        return inc - exp

    def get_summary(self, user_id: int, start: datetime, end: datetime) -> dict:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT type, COALESCE(SUM(amount),0) AS total
            FROM transactions
            WHERE user_id=? AND created_at BETWEEN ? AND ?
            GROUP BY type
            """,
            (user_id, start.isoformat(), end.isoformat()),
        )
        data = {"income": 0.0, "expense": 0.0}
        for r in cur.fetchall():
            data[r["type"]] = r["total"]
        return data

    def get_category_summary_month(self, user_id: int, year: int, month: int):
        first = datetime(year, month, 1)
        if month == 12:
            last = datetime(year + 1, 1, 1)
        else:
            last = datetime(year, month + 1, 1)
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT category, COALESCE(SUM(amount),0) AS total
            FROM transactions
            WHERE user_id=? AND type='expense'
              AND created_at BETWEEN ? AND ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (user_id, first.isoformat(), last.isoformat()),
        )
        return cur.fetchall()

    def get_recent_transactions(self, user_id: int, limit: int = 5):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT id, type, amount, category, note, wallet_id, created_at
            FROM transactions
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return cur.fetchall()

    def get_transaction(self, user_id: int, tx_id: int):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT id, type, amount, category, note, wallet_id, created_at
            FROM transactions
            WHERE id=? AND user_id=?
            """,
            (tx_id, user_id),
        )
        return cur.fetchone()

    def delete_transaction(self, user_id: int, tx_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM transactions WHERE id=? AND user_id=?",
            (tx_id, user_id),
        )
        self.conn.commit()

    def update_transaction_amount(self, user_id: int, tx_id: int, amount: float):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE transactions SET amount=? WHERE id=? AND user_id=?",
            (amount, tx_id, user_id),
        )
        self.conn.commit()

    def update_transaction_category(self, user_id: int, tx_id: int, category: str):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE transactions SET category=? WHERE id=? AND user_id=?",
            (category, tx_id, user_id),
        )
        self.conn.commit()

    def update_transaction_note(self, user_id: int, tx_id: int, note: str):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE transactions SET note=? WHERE id=? AND user_id=?",
            (note, tx_id, user_id),
        )
        self.conn.commit()

    # --- LIMITS ---

    def set_limit(self, user_id: int, category: str, period: str, limit_amount: float):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT id FROM limits
            WHERE user_id=? AND category=? AND period=?
            """,
            (user_id, category, period),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE limits SET limit_amount=? WHERE id=?",
                (limit_amount, row["id"]),
            )
        else:
            cur.execute(
                """
                INSERT INTO limits (user_id, category, period, limit_amount)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, category, period, limit_amount),
            )
        self.conn.commit()

    def get_limit(self, user_id: int, category: str, period: str):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT limit_amount FROM limits
            WHERE user_id=? AND category=? AND period=?
            """,
            (user_id, category, period),
        )
        row = cur.fetchone()
        return row["limit_amount"] if row else None

    def get_spent_in_month_for_category(
        self, user_id: int, category: str, year: int, month: int
    ) -> float:
        first = datetime(year, month, 1)
        if month == 12:
            last = datetime(year + 1, 1, 1)
        else:
            last = datetime(year, month + 1, 1)
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS total
            FROM transactions
            WHERE user_id=? AND type='expense'
              AND category=?
              AND created_at BETWEEN ? AND ?
            """,
            (user_id, category, first.isoformat(), last.isoformat()),
        )
        row = cur.fetchone()
        return row["total"] if row else 0.0

    # --- SAVING GOALS & BUDGET ---

    def create_saving_goal(self, user_id: int, name: str, target_amount: float):
        now = datetime.utcnow().isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO saving_goals (user_id, name, target_amount, current_amount, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (user_id, name, target_amount, now),
        )
        self.conn.commit()

    def get_saving_goals(self, user_id: int):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT id, name, target_amount, current_amount
            FROM saving_goals
            WHERE user_id=?
            ORDER BY id DESC
            """,
            (user_id,),
        )
        return cur.fetchall()

    def get_goal(self, goal_id: int):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT id, user_id, name, target_amount, current_amount
            FROM saving_goals
            WHERE id=?
            """,
            (goal_id,),
        )
        return cur.fetchone()

    def update_goal_amount(self, goal_id: int, new_amount: float):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE saving_goals SET current_amount=? WHERE id=?",
            (new_amount, goal_id),
        )
        self.conn.commit()

    def add_goal_transaction(self, goal_id: int, tx_type: str, amount: float, note: str):
        now = datetime.utcnow().isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO saving_goal_transactions (goal_id, type, amount, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (goal_id, tx_type, amount, note, now),
        )
        self.conn.commit()

    def save_budget(
        self,
        user_id: int,
        total_income: float,
        essential: float,
        long_term: float,
        invest: float,
        personal: float,
    ):
        now = datetime.utcnow().isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO budgets (user_id, total_income, essential, long_term, invest, personal, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, total_income, essential, long_term, invest, personal, now),
        )
        self.conn.commit()

    # --- EXPORT CSV ---

    def get_all_transactions_for_export(self, user_id: int):
        """
        Lấy toàn bộ giao dịch của user, join tên ví để export CSV.
        """
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT
                t.id,
                t.created_at,
                t.type,
                t.amount,
                t.category,
                t.note,
                w.name AS wallet_name
            FROM transactions t
            LEFT JOIN wallets w ON t.wallet_id = w.id
            WHERE t.user_id = ?
            ORDER BY t.created_at ASC, t.id ASC
            """,
            (user_id,),
        )
        return cur.fetchall()

    def get_transactions_for_month_export(self, user_id: int, year: int, month: int):
        """
        Lấy toàn bộ giao dịch trong 1 tháng, join tên ví để export CSV.
        """
        first = datetime(year, month, 1)
        if month == 12:
            last = datetime(year + 1, 1, 1)
        else:
            last = datetime(year, month + 1, 1)

        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT
                t.id,
                t.created_at,
                t.type,
                t.amount,
                t.category,
                t.note,
                w.name AS wallet_name
            FROM transactions t
            LEFT JOIN wallets w ON t.wallet_id = w.id
            WHERE t.user_id = ?
              AND t.created_at BETWEEN ? AND ?
            ORDER BY t.created_at ASC, t.id ASC
            """,
            (user_id, first.isoformat(), last.isoformat()),
        )
        return cur.fetchall()

    def get_transactions_for_wallet_export(self, user_id: int, wallet_id: int):
        """
        Lấy toàn bộ giao dịch của user cho 1 ví cụ thể, để export CSV.
        """
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT
                t.id,
                t.created_at,
                t.type,
                t.amount,
                t.category,
                t.note,
                w.name AS wallet_name
            FROM transactions t
            LEFT JOIN wallets w ON t.wallet_id = w.id
            WHERE t.user_id = ?
              AND t.wallet_id = ?
            ORDER BY t.created_at ASC, t.id ASC
            """,
            (user_id, wallet_id),
        )
        return cur.fetchall()


db = Database(DB_PATH)

# ==========================
# FSM STATES
# ==========================


class AddTransactionStates(StatesGroup):
    choosing_type = State()
    entering_amount_note = State()
    choosing_category = State()
    choosing_wallet = State()


class CreateGoalStates(StatesGroup):
    entering_name = State()
    entering_target = State()


class GoalMoneyStates(StatesGroup):
    entering_amount = State()
    entering_note = State()


class BudgetStates(StatesGroup):
    entering_income = State()


class LimitStates(StatesGroup):
    choosing_category = State()
    entering_amount = State()


class CategoryStates(StatesGroup):
    choosing_type = State()
    entering_name = State()


class EditTransactionStates(StatesGroup):
    choosing_field = State()
    editing_field = State()


class SalaryStates(StatesGroup):
    entering_amount = State()


class ExportMonthStates(StatesGroup):
    entering_period = State()


class TransferStates(StatesGroup):
    choosing_from_wallet = State()
    choosing_to_wallet = State()
    entering_amount = State()
    entering_note = State()


class WalletAddStates(StatesGroup):
    entering_name = State()


# context tạm
user_goal_action_context: dict[int, dict] = {}
user_edit_tx_context: dict[int, dict] = {}

# ==========================
# KEYBOARD
# ==========================


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Thu nhập"),
                KeyboardButton(text="➖ Chi tiêu"),
            ],
            [
                KeyboardButton(text="🎯 Mục tiêu"),
                KeyboardButton(text="📊 Báo cáo"),
            ],
            [
                KeyboardButton(text="📐 Ngân sách 4-2-2-2"),
                KeyboardButton(text="📝 Danh mục"),
            ],
            [
                KeyboardButton(text="💰 Giao dịch gần đây"),
                KeyboardButton(text="💼 Ví & số dư"),
            ],
        ],
        resize_keyboard=True,
    )


def income_expense_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Thu nhập", callback_data="add_tx_type|income"),
                InlineKeyboardButton(text="💸 Chi tiêu", callback_data="add_tx_type|expense"),
            ]
        ]
    )


def budget_after_calc_kb(total_income: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Lưu ghi chú ngân sách",
                    callback_data=f"budget_note|{total_income}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Tạo mục tiêu (2 khoản 20%)",
                    callback_data=f"budget_goals|{total_income}",
                )
            ],
        ]
    )


def report_menu_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📆 Hôm nay", callback_data="report_today"),
                InlineKeyboardButton(text="📅 7 ngày qua", callback_data="report_7days"),
            ],
            [
                InlineKeyboardButton(text="🗓 Tháng này", callback_data="report_month"),
                InlineKeyboardButton(text="📊 Theo danh mục", callback_data="report_categories"),
            ],
            [
                InlineKeyboardButton(text="💼 Số dư hiện tại", callback_data="report_balance"),
            ],
        ]
    )


def goals_inline_kb(goals_rows) -> InlineKeyboardMarkup:
    rows = []
    for row in goals_rows:
        goal_id = row["id"]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"➕ Gửi tiền #{goal_id}",
                    callback_data=f"goal_deposit|{goal_id}",
                ),
                InlineKeyboardButton(
                    text=f"➖ Rút tiền #{goal_id}",
                    callback_data=f"goal_withdraw|{goal_id}",
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ Tạo mục tiêu mới", callback_data="goal_create_new")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def transactions_inline_kb(tx_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Sửa", callback_data=f"tx_edit|{tx_id}"
                ),
                InlineKeyboardButton(
                    text="🗑 Xoá", callback_data=f"tx_delete|{tx_id}"
                ),
            ]
        ]
    )


# ==========================
# ROUTER & HANDLERS
# ==========================

router = Router()

# ---------- /start ----------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await state.clear()

    # Tóm tắt nhanh hôm nay
    today = date.today()
    start_today = datetime(today.year, today.month, today.day)
    end_today = start_today + timedelta(days=1)
    summary_today = db.get_summary(user_id, start_today, end_today)
    income_today = summary_today["income"]
    expense_today = summary_today["expense"]

    total_balance = db.get_balance(user_id)
    db.ensure_default_wallets(user_id)
    wallets = db.get_wallets(user_id)

    wallet_lines = []
    for w in wallets[:3]:  # chỉ hiện tối đa 3 ví cho gọn
        bal = db.get_wallet_balance(user_id, w["id"])
        wallet_lines.append(f"• {w['name']}: `{bal:,.0f}`")

    wallet_text = "\n".join(wallet_lines) if wallet_lines else "Chưa có ví nào."

    text = (
        f"Xin chào *{message.from_user.full_name}* 👋\n\n"
        "Mình là bot quản lý tài chính cá nhân.\n\n"
        "📊 *Tóm tắt nhanh hôm nay:*\n"
        f"• Thu nhập: `{income_today:,.0f}`\n"
        f"• Chi tiêu: `{expense_today:,.0f}`\n"
        f"• Số dư (tổng thu - chi): `{total_balance:,.0f}`\n\n"
        "💼 *Một vài ví gần đây:*\n"
        f"{wallet_text}\n\n"
        "Bạn có thể:\n"
        "• Ghi *Thu nhập* hoặc *Chi tiêu* (hỗ trợ nhập kiểu `35k ăn sáng`)\n"
        "• Ghi lương bằng /salary để tự chia 4-2-2-2 vào 4 ví\n"
        "• Tạo & theo dõi *Mục tiêu tiết kiệm* (/goals, /goals_add)\n"
        "• Xem *báo cáo* bằng /report hoặc /insights\n"
        "• Xuất dữ liệu CSV bằng /export, /export_month, /export_wallet\n\n"
        "Dùng các nút bên dưới hoặc gõ /help để xem chi tiết."
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ---------- /help ----------


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "🆘 *Hướng dẫn sử dụng bot*\n\n"
        "Các lệnh chính:\n"
        "• /add – Thêm giao dịch Thu nhập / Chi tiêu\n"
        "• /salary – Ghi lương và tự chia 4-2-2-2 vào 4 ví\n"
        "• /wallets – Xem số dư từng ví\n"
        "• /wallets_add – Tạo ví mới\n"
        "• /transfer – Chuyển tiền giữa các ví\n"
        "• /transactions – Xem & quản lý giao dịch gần đây\n"
        "• /report – Xem báo cáo tài chính\n"
        "• /insights – Phân tích chi tiêu thông minh\n"
        "• /goals – Quản lý mục tiêu tiết kiệm\n"
        "• /goals_add – Tạo mục tiêu tiết kiệm mới\n"
        "• /budget – Tính ngân sách 4-2-2-2\n"
        "• /categories – Quản lý danh mục thu/chi\n"
        "• /limit – Đặt hạn mức chi tiêu theo danh mục\n"
        "• /export – Xuất toàn bộ giao dịch ra file CSV\n"
        "• /export_month – Xuất giao dịch theo tháng\n"
        "• /export_wallet – Xuất giao dịch theo từng ví\n"
        "• /backup – Sao lưu file database\n\n"
        "Bạn cũng có thể dùng menu nhanh bên dưới để thao tác."
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ---------- MENU REPLY BUTTONS ----------


@router.message(F.text == "➕ Thu nhập")
async def add_income_btn(message: Message, state: FSMContext):
    await start_add_transaction(message, state, "income")


@router.message(F.text == "➖ Chi tiêu")
async def add_expense_btn(message: Message, state: FSMContext):
    await start_add_transaction(message, state, "expense")


@router.message(F.text == "🎯 Mục tiêu")
async def goals_btn(message: Message, state: FSMContext):
    await cmd_goals(message, state)


@router.message(F.text == "📊 Báo cáo")
async def report_btn(message: Message):
    await cmd_report(message)


@router.message(F.text == "📐 Ngân sách 4-2-2-2")
async def budget_btn(message: Message, state: FSMContext):
    await cmd_budget(message, state)


@router.message(F.text == "📝 Danh mục")
async def categories_btn(message: Message, state: FSMContext):
    await cmd_categories(message, state)


@router.message(F.text == "💰 Giao dịch gần đây")
async def tx_btn(message: Message):
    await cmd_transactions(message)


@router.message(F.text == "💼 Ví & số dư")
async def wallets_btn(message: Message):
    await cmd_wallets(message)


# ---------- /add ----------


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await state.set_state(AddTransactionStates.choosing_type)
    await message.answer(
        "Bạn muốn ghi *Thu nhập* hay *Chi tiêu*?\n"
        "Chọn bằng nút bên dưới:",
        reply_markup=income_expense_inline_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )


@router.callback_query(F.data.startswith("add_tx_type"))
async def cb_add_tx_type(call: CallbackQuery, state: FSMContext):
    _, tx_type = call.data.split("|", maxsplit=1)
    await state.update_data(tx_type=tx_type)
    await state.set_state(AddTransactionStates.entering_amount_note)
    label = "Thu nhập" if tx_type == "income" else "Chi tiêu"
    await call.message.edit_text(
        f"Nhập *{label}* theo dạng:\n"
        "- `200000`\n"
        "- `200k ăn trưa`\n"
        "- `1.5tr tiền nhà`\n\n"
        "Bạn chỉ cần gõ một dòng, mình sẽ tự hiểu số tiền và ghi chú.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer()


async def start_add_transaction(message: Message, state: FSMContext, tx_type: str):
    db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await state.set_state(AddTransactionStates.entering_amount_note)
    await state.update_data(tx_type=tx_type)
    label = "Thu nhập" if tx_type == "income" else "Chi tiêu"
    await message.answer(
        f"Nhập *{label}* theo dạng:\n"
        "- `200000`\n"
        "- `200k ăn trưa`\n"
        "- `1.5tr tiền nhà`\n\n"
        "Bạn chỉ cần gõ một dòng, mình sẽ tự hiểu số tiền và ghi chú.",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(AddTransactionStates.entering_amount_note)
async def add_tx_amount_note(message: Message, state: FSMContext):
    raw = message.text
    try:
        amount, note = extract_amount_and_note(raw)
        if amount <= 0:
            raise ValueError()
    except Exception:
        await message.answer(
            "❌ Không đọc được số tiền.\n"
            "Bạn thử lại ví dụ: `35k ăn sáng`, `150000 tiền điện`, `1.2tr tiền nhà`."
        )
        return

    await state.update_data(amount=amount, note=note)

    # hỏi danh mục
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    data = await state.get_data()
    tx_type = data["tx_type"]
    cat_rows = db.get_categories(user_id, tx_type)

    if not cat_rows:
        db.ensure_default_categories(user_id)
        cat_rows = db.get_categories(user_id, tx_type)

    buttons = []
    row = []
    for c in cat_rows:
        row.append(KeyboardButton(text=c["name"]))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([KeyboardButton(text="Khác")])

    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

    await state.set_state(AddTransactionStates.choosing_category)
    await message.answer(
        f"Số tiền: `{amount:,.0f}`\n"
        f"Ghi chú: {note or 'Không có'}\n\n"
        "Bây giờ hãy chọn hoặc nhập *Danh mục* cho giao dịch:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.message(AddTransactionStates.choosing_category)
async def add_tx_category(message: Message, state: FSMContext):
    category = message.text.strip()
    if not category:
        await message.answer("❌ Danh mục không được để trống, vui lòng nhập lại.")
        return

    await state.update_data(category=category)

    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.ensure_default_wallets(user_id)
    wallets = db.get_wallets(user_id)

    buttons = []
    row = []
    for w in wallets:
        row.append(KeyboardButton(text=f"{w['name']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

    await state.set_state(AddTransactionStates.choosing_wallet)
    await message.answer(
        "Chọn *Ví* cho giao dịch này (ví mà bạn muốn tiền ra/vào):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.message(AddTransactionStates.choosing_wallet)
async def add_tx_wallet(message: Message, state: FSMContext):
    wallet_name = message.text.strip()
    if not wallet_name:
        await message.answer("❌ Tên ví không được để trống, vui lòng chọn lại.")
        return

    data = await state.get_data()
    tx_type = data["tx_type"]
    amount = data["amount"]
    note = data["note"]
    category = data["category"]

    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    wallets = db.get_wallets(user_id)

    chosen = None
    for w in wallets:
        if w["name"].lower() == wallet_name.lower():
            chosen = w
            break
    if not chosen:
        await message.answer(
            "❌ Không tìm thấy ví này. Vui lòng chọn lại đúng tên từ danh sách.",
        )
        return

    wallet_id = chosen["id"]
    tx_id = db.add_transaction(user_id, tx_type, amount, category, note, wallet_id)
    await state.clear()

    warn_text = ""
    if tx_type == "expense":
        today = date.today()
        spent = db.get_spent_in_month_for_category(user_id, category, today.year, today.month)
        limit = db.get_limit(user_id, category, "month")
        if limit is not None and spent > limit:
            warn_text = (
                f"\n\n⚠️ *CẢNH BÁO:* Bạn đã *vượt hạn mức* chi cho danh mục *{category}* "
                f"trong tháng này.\n"
                f"Đã chi: `{spent:,.0f}` / Hạn mức: `{limit:,.0f}`"
            )

    label = "Thu nhập" if tx_type == "income" else "Chi tiêu"
    text = (
        f"✅ Đã ghi giao dịch #{tx_id}:\n\n"
        f"• Loại: *{label}*\n"
        f"• Số tiền: `{amount:,.0f}`\n"
        f"• Danh mục: *{category}*\n"
        f"• Ví: *{chosen['name']}*\n"
        f"• Ghi chú: {note or 'Không có'}\n"
        + warn_text +
        "\n\nDùng /wallets để xem số dư từng ví, hoặc /transactions để sửa/xoá."
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ---------- /budget – Quy tắc 4-2-2-2 ----------


@router.message(Command("budget"))
async def cmd_budget(message: Message, state: FSMContext):
    await state.set_state(BudgetStates.entering_income)
    await message.answer(
        "💰 *Tính ngân sách 4-2-2-2*\n\n"
        "Nhập *tổng lương / thu nhập hàng tháng* (ví dụ: `15tr`, `15000000`):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(BudgetStates.entering_income)
async def budget_income(message: Message, state: FSMContext):
    raw = message.text
    try:
        total_income, _ = extract_amount_and_note(raw)
        if total_income <= 0:
            raise ValueError()
    except Exception:
        await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại (ví dụ: `15tr`, `15000000`).")
        return

    await state.clear()
    essential = total_income * 0.4
    long_term = total_income * 0.2
    invest = total_income * 0.2
    personal = total_income * 0.2

    text = (
        "📐 *Phân bổ lương theo quy tắc 4-2-2-2*\n\n"
        f"• Tổng thu nhập: `{total_income:,.0f}`\n\n"
        "👉 Đề xuất phân bổ:\n"
        f"• 40% Chi tiêu thiết yếu: `{essential:,.0f}`\n"
        f"• 20% Tiết kiệm dài hạn: `{long_term:,.0f}`\n"
        f"• 20% Đầu tư & Tự do tài chính: `{invest:,.0f}`\n"
        f"• 20% Chi tiêu cá nhân & Phát triển: `{personal:,.0f}`\n\n"
        "Bạn muốn *lưu lại* hay *tạo mục tiêu tiết kiệm* từ 2 khoản 20%?"
    )
    await message.answer(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=budget_after_calc_kb(total_income),
    )


@router.callback_query(F.data.startswith("budget_note"))
async def cb_budget_note(call: CallbackQuery):
    _, total_str = call.data.split("|", maxsplit=1)
    try:
        total = float(total_str)
    except ValueError:
        await call.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return

    essential = total * 0.4
    long_term = total * 0.2
    invest = total * 0.2
    personal = total * 0.2

    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    db.save_budget(user_id, total, essential, long_term, invest, personal)

    await call.message.edit_text(
        "✅ Đã lưu *ghi chú ngân sách 4-2-2-2*.\n\n"
        "Bạn có thể tính lại bằng /budget bất cứ lúc nào.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer("Đã lưu ngân sách.")


@router.callback_query(F.data.startswith("budget_goals"))
async def cb_budget_goals(call: CallbackQuery):
    _, total_str = call.data.split("|", maxsplit=1)
    try:
        total = float(total_str)
    except ValueError:
        await call.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return

    long_term = total * 0.2
    invest = total * 0.2
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)

    db.create_saving_goal(user_id, "Tiết kiệm dài hạn (4-2-2-2)", long_term)
    db.create_saving_goal(user_id, "Đầu tư & Tự do tài chính (4-2-2-2)", invest)

    await call.message.edit_text(
        "✅ Đã tạo 2 *Mục tiêu tiết kiệm* từ 2 khoản 20%.\n"
        "Dùng /goals để xem & nạp/rút tiền.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer("Đã tạo mục tiêu từ ngân sách.")


# ---------- /salary – tự chia lương vào 4 ví ----------


@router.message(Command("salary"))
async def cmd_salary(message: Message, state: FSMContext):
    db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await state.set_state(SalaryStates.entering_amount)
    await message.answer(
        "💵 *Ghi lương và tự chia vào 4 ví (4-2-2-2)*\n\n"
        "Nhập *tổng lương/thu nhập* tháng này (ví dụ: `15tr`, `15000000`):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(SalaryStates.entering_amount)
async def salary_enter_amount(message: Message, state: FSMContext):
    raw = message.text
    try:
        total, _ = extract_amount_and_note(raw)
        if total <= 0:
            raise ValueError()
    except Exception:
        await message.answer("❌ Số tiền không hợp lệ, nhập lại (ví dụ: `15tr`, `15000000`).")
        return

    await state.clear()
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.ensure_default_wallets(user_id)
    wallets = db.get_wallets(user_id)

    purpose_map = {w["purpose"]: w for w in wallets}
    essential_w = purpose_map.get("4-2-2-2 essential")
    long_w = purpose_map.get("4-2-2-2 long_term")
    invest_w = purpose_map.get("4-2-2-2 invest")
    personal_w = purpose_map.get("4-2-2-2 personal")

    if not all([essential_w, long_w, invest_w, personal_w]):
        await message.answer(
            "❌ Không tìm thấy đủ 4 ví mặc định. Bạn hãy xoá DB cũ hoặc kiểm tra lại cấu hình ví.",
            reply_markup=main_menu_kb(),
        )
        return

    essential = total * 0.4
    long_term = total * 0.2
    invest = total * 0.2
    personal = total * 0.2

    # Ghi 4 giao dịch Thu nhập vào 4 ví
    db.add_transaction(
        user_id,
        "income",
        essential,
        "Lương - Chi tiêu thiết yếu",
        "",
        essential_w["id"],
    )
    db.add_transaction(
        user_id,
        "income",
        long_term,
        "Lương - Tiết kiệm dài hạn",
        "",
        long_w["id"],
    )
    db.add_transaction(
        user_id,
        "income",
        invest,
        "Lương - Đầu tư & Tự do tài chính",
        "",
        invest_w["id"],
    )
    db.add_transaction(
        user_id,
        "income",
        personal,
        "Lương - Chi tiêu cá nhân & Phát triển",
        "",
        personal_w["id"],
    )

    text = (
        "✅ Đã ghi lương và tự chia vào 4 ví theo 4-2-2-2:\n\n"
        f"• Tổng lương: `{total:,.0f}`\n\n"
        f"• {essential_w['name']}: `{essential:,.0f}` (40%)\n"
        f"• {long_w['name']}: `{long_term:,.0f}` (20%)\n"
        f"• {invest_w['name']}: `{invest:,.0f}` (20%)\n"
        f"• {personal_w['name']}: `{personal:,.0f}` (20%)\n\n"
        "Dùng /wallets để xem số dư từng ví, và khi ghi chi tiêu/thu nhập, hãy chọn đúng ví tương ứng mục đích."
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ---------- /wallets – xem số dư ví ----------


@router.message(Command("wallets"))
async def cmd_wallets(message: Message):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.ensure_default_wallets(user_id)
    wallets = db.get_wallets(user_id)
    if not wallets:
        await message.answer(
            "Hiện bạn chưa có ví nào. (Lỗi bất thường, thử /start lại hoặc xoá DB nếu đang dev.)",
            reply_markup=main_menu_kb(),
        )
        return

    lines = ["💼 *Các ví của bạn:*\n"]
    for w in wallets:
        bal = db.get_wallet_balance(user_id, w["id"])
        lines.append(
            f"• #{w['id']} – *{w['name']}*\n"
            f"  Số dư: `{bal:,.0f}`\n"
        )

    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ---------- /wallets_add – tạo ví mới ----------


@router.message(Command("wallets_add"))
async def cmd_wallets_add(message: Message, state: FSMContext):
    db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await state.set_state(WalletAddStates.entering_name)
    await message.answer(
        "💼 *Tạo ví mới*\n\n"
        "Nhập tên ví bạn muốn tạo (ví dụ: `Momo`, `Tiền mặt`, `Thẻ tín dụng`):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(WalletAddStates.entering_name)
async def wallets_add_enter_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Tên ví không được để trống, vui lòng nhập lại.")
        return

    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.add_wallet(user_id, name, purpose="")
    await state.clear()
    await message.answer(
        f"✅ Đã tạo ví mới: *{name}*.\nDùng /wallets để xem danh sách ví.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ---------- /transfer – chuyển tiền giữa ví ----------


@router.message(Command("transfer"))
async def cmd_transfer(message: Message, state: FSMContext):
    """
    Chuyển tiền giữa các ví: ghi 1 giao dịch chi ở ví nguồn, 1 giao dịch thu ở ví đích.
    """
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.ensure_default_wallets(user_id)
    wallets = db.get_wallets(user_id)
    if len(wallets) < 2:
        await message.answer(
            "Bạn cần ít nhất 2 ví để chuyển tiền.\n"
            "Dùng /wallets_add để tạo thêm ví.",
            reply_markup=main_menu_kb(),
        )
        return

    buttons = []
    row = []
    for w in wallets:
        row.append(KeyboardButton(text=w["name"]))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

    await state.set_state(TransferStates.choosing_from_wallet)
    await message.answer(
        "🔁 *Chuyển tiền giữa ví*\n\n"
        "Bước 1: Chọn *ví nguồn* (ví bị trừ tiền):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.message(TransferStates.choosing_from_wallet)
async def transfer_choose_from(message: Message, state: FSMContext):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    wallets = db.get_wallets(user_id)
    name = (message.text or "").strip()

    from_wallet = None
    for w in wallets:
        if w["name"].lower() == name.lower():
            from_wallet = w
            break

    if not from_wallet:
        await message.answer("❌ Không tìm thấy ví này, vui lòng chọn lại từ danh sách.")
        return

    await state.update_data(from_wallet_id=from_wallet["id"])
    # Chọn ví đích
    buttons = []
    row = []
    for w in wallets:
        if w["id"] == from_wallet["id"]:
            continue
        row.append(KeyboardButton(text=w["name"]))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

    await state.set_state(TransferStates.choosing_to_wallet)
    await message.answer(
        "Bước 2: Chọn *ví đích* (ví được cộng tiền):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.message(TransferStates.choosing_to_wallet)
async def transfer_choose_to(message: Message, state: FSMContext):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    wallets = db.get_wallets(user_id)
    data = await state.get_data()
    from_wallet_id = data["from_wallet_id"]

    name = (message.text or "").strip()
    to_wallet = None
    for w in wallets:
        if w["name"].lower() == name.lower():
            to_wallet = w
            break

    if not to_wallet or to_wallet["id"] == from_wallet_id:
        await message.answer("❌ Ví đích không hợp lệ, vui lòng chọn lại.")
        return

    await state.update_data(to_wallet_id=to_wallet["id"])

    await state.set_state(TransferStates.entering_amount)
    await message.answer(
        "Bước 3: Nhập *số tiền cần chuyển* (ví dụ: `500k`, `1tr`, `1000000`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


@router.message(TransferStates.entering_amount)
async def transfer_enter_amount(message: Message, state: FSMContext):
    raw = message.text
    try:
        amount, _ = extract_amount_and_note(raw)
        if amount <= 0:
            raise ValueError()
    except Exception:
        await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại.")
        return

    await state.update_data(amount=amount)
    await state.set_state(TransferStates.entering_note)
    await message.answer(
        "Bước 4: Nhập ghi chú cho lần chuyển (hoặc gõ `-` nếu không có):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(TransferStates.entering_note)
async def transfer_enter_note(message: Message, state: FSMContext):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    data = await state.get_data()
    from_wallet_id = data["from_wallet_id"]
    to_wallet_id = data["to_wallet_id"]
    amount = data["amount"]

    note = (message.text or "").strip()
    if note == "-":
        note = ""

    from_wallet = db.get_wallet(user_id, from_wallet_id)
    to_wallet = db.get_wallet(user_id, to_wallet_id)

    # Ghi 1 giao dịch "chi" ở ví nguồn
    db.add_transaction(
        user_id,
        "expense",
        amount,
        f"Chuyển sang ví {to_wallet['name']}",
        note,
        from_wallet_id,
    )

    # Ghi 1 giao dịch "thu" ở ví đích
    db.add_transaction(
        user_id,
        "income",
        amount,
        f"Chuyển từ ví {from_wallet['name']}",
        note,
        to_wallet_id,
    )

    await state.clear()
    await message.answer(
        "✅ Đã chuyển tiền giữa ví:\n\n"
        f"• Từ: *{from_wallet['name']}*\n"
        f"• Sang: *{to_wallet['name']}*\n"
        f"• Số tiền: `{amount:,.0f}`\n"
        f"• Ghi chú: {note or 'Không có'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ---------- /report ----------


@router.message(Command("report"))
async def cmd_report(message: Message):
    db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await message.answer(
        "📊 *Báo cáo tài chính*\n\nChọn loại báo cáo:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=report_menu_inline_kb(),
    )


@router.callback_query(F.data == "report_today")
async def cb_report_today(call: CallbackQuery):
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    now = datetime.utcnow()
    start = datetime(now.year, now.month, now.day)
    end = start + timedelta(days=1)
    data = db.get_summary(user_id, start, end)
    income = data["income"]
    expense = data["expense"]
    balance = income - expense
    text = (
        "📆 *Báo cáo hôm nay*\n\n"
        f"• Thu nhập: `{income:,.0f}`\n"
        f"• Chi tiêu: `{expense:,.0f}`\n"
        f"• Chênh lệch: `{balance:,.0f}`"
    )
    await call.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=report_menu_inline_kb())
    await call.answer()


@router.callback_query(F.data == "report_7days")
async def cb_report_7days(call: CallbackQuery):
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    end = datetime.utcnow()
    start = end - timedelta(days=7)
    data = db.get_summary(user_id, start, end)
    income = data["income"]
    expense = data["expense"]
    balance = income - expense
    text = (
        "📅 *Báo cáo 7 ngày qua*\n\n"
        f"• Thu nhập: `{income:,.0f}`\n"
        f"• Chi tiêu: `{expense:,.0f}`\n"
        f"• Chênh lệch: `{balance:,.0f}`"
    )
    await call.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=report_menu_inline_kb())
    await call.answer()


@router.callback_query(F.data == "report_month")
async def cb_report_month(call: CallbackQuery):
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    today = date.today()
    start = datetime(today.year, today.month, 1)
    end = datetime(today.year + (1 if today.month == 12 else 0),
                   1 if today.month == 12 else today.month + 1,
                   1)
    data = db.get_summary(user_id, start, end)
    income = data["income"]
    expense = data["expense"]
    balance = income - expense
    text = (
        "🗓 *Báo cáo tháng này*\n\n"
        f"• Thu nhập: `{income:,.0f}`\n"
        f"• Chi tiêu: `{expense:,.0f}`\n"
        f"• Chênh lệch: `{balance:,.0f}`"
    )
    await call.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=report_menu_inline_kb())
    await call.answer()


@router.callback_query(F.data == "report_categories")
async def cb_report_categories(call: CallbackQuery):
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    today = date.today()
    rows = db.get_category_summary_month(user_id, today.year, today.month)
    if not rows:
        text = "📊 *Thống kê theo danh mục (tháng này)*\n\nChưa có chi tiêu nào."
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=report_menu_inline_kb(),
        )
        await call.answer()
        return

    lines = ["📊 *Thống kê chi tiêu theo danh mục (tháng này)*\n"]
    max_val = max(r["total"] for r in rows) or 1
    BAR_WIDTH = 20

    for r in rows:
        cat = r["category"]
        val = r["total"]
        bar_len = int(val / max_val * BAR_WIDTH) if max_val > 0 else 0
        bar = "█" * bar_len
        lines.append(f"{cat:15} {bar} `{val:,.0f}`")

    # Top 3 khoản chi lớn nhất
    top3 = rows[:3]
    lines.append("\n🔥 *Top 3 danh mục chi lớn nhất:*")
    for i, r in enumerate(top3, start=1):
        lines.append(f"{i}. {r['category']}: `{r['total']:,.0f}`")

    text = "\n".join(lines)
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=report_menu_inline_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "report_balance")
async def cb_report_balance(call: CallbackQuery):
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    balance = db.get_balance(user_id)
    text = (
        "💼 *Số dư hiện tại (tổng thu nhập - tổng chi tiêu)*\n\n"
        f"• Số dư ước tính: `{balance:,.0f}`"
    )
    await call.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=report_menu_inline_kb())
    await call.answer()


# ---------- /insights – phân tích chi tiêu ----------


@router.message(Command("insights"))
async def cmd_insights(message: Message):
    """
    Phân tích nhanh: so sánh 30 ngày gần nhất với 30 ngày trước đó
    + top danh mục đang chi nhiều trong tháng này.
    """
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    now = datetime.utcnow()

    recent_end = now
    recent_start = recent_end - timedelta(days=30)
    prev_end = recent_start
    prev_start = prev_end - timedelta(days=30)

    recent = db.get_summary(user_id, recent_start, recent_end)
    prev = db.get_summary(user_id, prev_start, prev_end)

    recent_exp = recent["expense"]
    prev_exp = prev["expense"]

    diff = recent_exp - prev_exp
    trend = "tăng" if diff > 0 else "giảm" if diff < 0 else "không đổi"
    diff_abs = abs(diff)

    today = date.today()
    cats = db.get_category_summary_month(user_id, today.year, today.month)

    lines = ["📈 *Phân tích chi tiêu (insights)*\n"]
    lines.append(
        f"• 30 ngày gần nhất: Chi tiêu `{recent_exp:,.0f}`\n"
        f"• 30 ngày trước đó: `{prev_exp:,.0f}`"
    )

    if diff != 0:
        lines.append(
            f"➡️ Bạn đang chi *{trend}* khoảng `{diff_abs:,.0f}` so với 30 ngày trước."
        )
    else:
        lines.append("➡️ Chi tiêu của bạn *gần như không đổi* so với 30 ngày trước.")

    if cats:
        lines.append("\n🔥 *Danh mục chi nhiều nhất tháng này:*")
        top = cats[0]
        lines.append(f"• {top['category']}: `{top['total']:,.0f}`")
        if len(cats) >= 3:
            lines.append("\n📌 Gợi ý:")
            lines.append(
                f"- Theo dõi kỹ danh mục *{top['category']}* trong vài tuần tới.\n"
                "- Cân nhắc đặt hạn mức bằng /limit nếu bạn thấy mục này hay bị vượt."
            )

    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ---------- /goals ----------


@router.message(Command("goals"))
async def cmd_goals(message: Message, state: FSMContext):
    await state.clear()
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    goals = db.get_saving_goals(user_id)
    if not goals:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Tạo mục tiêu mới", callback_data="goal_create_new")]
            ]
        )
        await message.answer(
            "🎯 *Mục tiêu tiết kiệm*\n\n"
            "Hiện bạn chưa có mục tiêu nào.\n"
            "Dùng /goals_add hoặc bấm nút dưới để tạo mới.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return
    lines = ["🎯 *Danh sách mục tiêu tiết kiệm*\n"]
    for g in goals:
        goal_id = g["id"]
        name = g["name"]
        target = g["target_amount"]
        current = g["current_amount"]
        percent = (current / target * 100) if target > 0 else 0
        lines.append(
            f"• #{goal_id} – *{name}*\n"
            f"  Tiến độ: `{current:,.0f} / {target:,.0f}` (~{percent:.1f}%)\n"
        )
    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=goals_inline_kb(goals),
    )


@router.message(Command("goals_add"))
async def cmd_goals_add(message: Message, state: FSMContext):
    await state.set_state(CreateGoalStates.entering_name)
    await message.answer(
        "🎯 Tạo *Mục tiêu tiết kiệm* mới\n\n"
        "Bước 1: Nhập *tên mục tiêu* (ví dụ: \"Quỹ khẩn cấp 6 tháng\", \"Du lịch Nhật Bản\").",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.callback_query(F.data == "goal_create_new")
async def cb_goal_create_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(CreateGoalStates.entering_name)
    await call.message.edit_text(
        "🎯 Tạo *Mục tiêu tiết kiệm* mới\n\n"
        "Bước 1: Nhập *tên mục tiêu* (ví dụ: \"Quỹ khẩn cấp\", \"Du lịch Nhật Bản\").",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer()


@router.message(CreateGoalStates.entering_name)
async def goal_enter_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Tên mục tiêu không được để trống, vui lòng nhập lại.")
        return
    await state.update_data(goal_name=name)
    await state.set_state(CreateGoalStates.entering_target)
    await message.answer(
        "Bước 2: Nhập *số tiền cần đạt* (ví dụ: `50tr`, `50000000`):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(CreateGoalStates.entering_target)
async def goal_enter_target(message: Message, state: FSMContext):
    raw = message.text
    try:
        target, _ = extract_amount_and_note(raw)
        if target <= 0:
            raise ValueError()
    except Exception:
        await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại (ví dụ: `50tr`, `50000000`).")
        return
    data = await state.get_data()
    name = data["goal_name"]
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.create_saving_goal(user_id, name, target)
    await state.clear()
    await message.answer(
        f"✅ Đã tạo mục tiêu *{name}* với số tiền cần đạt `{target:,.0f}`.\n"
        "Dùng /goals để xem danh sách và nạp/rút tiền.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("goal_deposit"))
async def cb_goal_deposit(call: CallbackQuery, state: FSMContext):
    _, goal_id_str = call.data.split("|", maxsplit=1)
    goal_id = int(goal_id_str)
    goal = db.get_goal(goal_id)
    if not goal:
        await call.answer("Mục tiêu không tồn tại.", show_alert=True)
        return
    user_goal_action_context[call.from_user.id] = {
        "goal_id": goal_id,
        "action": "deposit",
    }
    await state.set_state(GoalMoneyStates.entering_amount)
    await call.message.edit_text(
        f"➕ *Gửi tiền* vào mục tiêu *{goal['name']}*\n\n"
        "Nhập số tiền (ví dụ: `1tr`, `1000000`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer()


@router.callback_query(F.data.startswith("goal_withdraw"))
async def cb_goal_withdraw(call: CallbackQuery, state: FSMContext):
    _, goal_id_str = call.data.split("|", maxsplit=1)
    goal_id = int(goal_id_str)
    goal = db.get_goal(goal_id)
    if not goal:
        await call.answer("Mục tiêu không tồn tại.", show_alert=True)
        return
    user_goal_action_context[call.from_user.id] = {
        "goal_id": goal_id,
        "action": "withdraw",
    }
    await state.set_state(GoalMoneyStates.entering_amount)
    await call.message.edit_text(
        f"➖ *Rút tiền* từ mục tiêu *{goal['name']}*\n\n"
        "Nhập số tiền (ví dụ: `500k`, `500000`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer()


@router.message(GoalMoneyStates.entering_amount)
async def goal_money_amount(message: Message, state: FSMContext):
    ctx = user_goal_action_context.get(message.from_user.id)
    if not ctx:
        await state.clear()
        await message.answer("Phiên thao tác mục tiêu đã hết hạn. Dùng /goals để thử lại.")
        return
    raw = message.text
    try:
        amount, _ = extract_amount_and_note(raw)
        if amount <= 0:
            raise ValueError()
    except Exception:
        await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại.")
        return
    await state.update_data(amount=amount)
    await state.set_state(GoalMoneyStates.entering_note)
    await message.answer(
        "Nhập ghi chú cho lần gửi/rút (hoặc gõ `-` nếu không có):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(GoalMoneyStates.entering_note)
async def goal_money_note(message: Message, state: FSMContext):
    ctx = user_goal_action_context.get(message.from_user.id)
    if not ctx:
        await state.clear()
        await message.answer("Phiên thao tác mục tiêu đã hết hạn. Dùng /goals để thử lại.")
        return
    data = await state.get_data()
    amount = data.get("amount")
    note = message.text.strip()
    if note == "-":
        note = ""
    goal = db.get_goal(ctx["goal_id"])
    if not goal:
        await message.answer("Mục tiêu không tồn tại.")
        await state.clear()
        return

    action = ctx["action"]
    current = goal["current_amount"]
    if action == "deposit":
        new_amount = current + amount
        db.update_goal_amount(goal["id"], new_amount)
        db.add_goal_transaction(goal["id"], "deposit", amount, note)
        msg = (
            f"✅ Đã *gửi* `{amount:,.0f}` vào mục tiêu *{goal['name']}*.\n"
            f"Số tiền hiện tại: `{new_amount:,.0f} / {goal['target_amount']:,.0f}`"
        )
    else:
        if amount > current:
            await message.answer(
                f"❌ Bạn chỉ có thể rút tối đa `{current:,.0f}` (số đang có trong mục tiêu)."
            )
            return
        new_amount = current - amount
        db.update_goal_amount(goal["id"], new_amount)
        db.add_goal_transaction(goal["id"], "withdraw", amount, note)
        msg = (
            f"✅ Đã *rút* `{amount:,.0f}` từ mục tiêu *{goal['name']}*.\n"
            f"Số tiền còn lại: `{new_amount:,.0f} / {goal['target_amount']:,.0f}`"
        )
    await state.clear()
    user_goal_action_context.pop(message.from_user.id, None)
    await message.answer(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ---------- /transactions – xem & sửa/xoá ----------


@router.message(Command("transactions"))
async def cmd_transactions(message: Message):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    rows = db.get_recent_transactions(user_id, limit=5)
    if not rows:
        await message.answer(
            "💰 *Giao dịch gần đây*\n\nChưa có giao dịch nào.\nDùng /add hoặc các nút Thu nhập / Chi tiêu để thêm.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    lines = ["💰 *5 giao dịch gần nhất*:\n"]
    for r in rows:
        s_type = "➕" if r["type"] == "income" else "➖"
        created = r["created_at"][:16].replace("T", " ")
        lines.append(
            f"#{r['id']} {s_type} `{r['amount']:,.0f}` – *{r['category']}*\n"
            f"   {r['note'] or 'Không ghi chú'}\n"
            f"   _{created} UTC_\n"
        )
    text = "\n".join(lines) + "\nChạm vào nút dưới mỗi giao dịch để *sửa* hoặc *xoá*."
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)
    # Gửi riêng từng giao dịch với inline keyboard sửa/xoá
    for r in rows:
        s_type = "Thu nhập" if r["type"] == "income" else "Chi tiêu"
        wallet_name = ""
        if r["wallet_id"] is not None:
            w = db.get_wallet(user_id, r["wallet_id"])
            wallet_name = w["name"] if w else ""
        msg = (
            f"#{r['id']} – *{s_type}*\n"
            f"Số tiền: `{r['amount']:,.0f}`\n"
            f"Danh mục: *{r['category']}*\n"
            f"Ví: *{wallet_name or 'Không xác định'}*\n"
            f"Ghi chú: {r['note'] or 'Không có'}"
        )
        await message.answer(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=transactions_inline_kb(r["id"]),
        )


@router.callback_query(F.data.startswith("tx_delete"))
async def cb_tx_delete(call: CallbackQuery):
    _, tx_id_str = call.data.split("|", maxsplit=1)
    tx_id = int(tx_id_str)
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    tx = db.get_transaction(user_id, tx_id)
    if not tx:
        await call.answer("Giao dịch không tồn tại.", show_alert=True)
        return
    db.delete_transaction(user_id, tx_id)
    await call.message.edit_text(
        f"🗑 Đã xoá giao dịch #{tx_id}.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer("Đã xoá giao dịch.")


@router.callback_query(F.data.startswith("tx_edit"))
async def cb_tx_edit(call: CallbackQuery, state: FSMContext):
    _, tx_id_str = call.data.split("|", maxsplit=1)
    tx_id = int(tx_id_str)
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    tx = db.get_transaction(user_id, tx_id)
    if not tx:
        await call.answer("Giao dịch không tồn tại.", show_alert=True)
        return
    user_edit_tx_context[call.from_user.id] = {"tx_id": tx_id}
    await state.set_state(EditTransactionStates.choosing_field)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Sửa số tiền", callback_data="edit_field|amount"),
            ],
            [
                InlineKeyboardButton(text="📂 Sửa danh mục", callback_data="edit_field|category"),
            ],
            [
                InlineKeyboardButton(text="📝 Sửa ghi chú", callback_data="edit_field|note"),
            ],
        ]
    )
    await call.message.edit_text(
        f"✏️ *Sửa giao dịch #{tx_id}*\n\n"
        f"Số tiền hiện tại: `{tx['amount']:,.0f}`\n"
        f"Danh mục: *{tx['category']}*\n"
        f"Ghi chú: {tx['note'] or 'Không có'}\n\n"
        "Chọn phần bạn muốn sửa:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("edit_field"))
async def cb_edit_field(call: CallbackQuery, state: FSMContext):
    _, field = call.data.split("|", maxsplit=1)
    ctx = user_edit_tx_context.get(call.from_user.id)
    if not ctx:
        await call.answer("Phiên sửa đã hết hạn. Dùng /transactions để chọn lại.", show_alert=True)
        return
    ctx["field"] = field
    user_edit_tx_context[call.from_user.id] = ctx
    await state.set_state(EditTransactionStates.editing_field)

    if field == "amount":
        await call.message.edit_text(
            "Nhập *số tiền mới* cho giao dịch (ví dụ: `200k`, `1.5tr`, `200000`):",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif field == "category":
        await call.message.edit_text(
            "Nhập *danh mục mới* cho giao dịch:",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await call.message.edit_text(
            "Nhập *ghi chú mới* cho giao dịch (hoặc `-` để xoá ghi chú):",
            parse_mode=ParseMode.MARKDOWN,
        )
    await call.answer()


@router.message(EditTransactionStates.editing_field)
async def edit_tx_field_value(message: Message, state: FSMContext):
    ctx = user_edit_tx_context.get(message.from_user.id)
    if not ctx:
        await state.clear()
        await message.answer("Phiên sửa đã hết hạn. Dùng /transactions để chọn lại.")
        return
    tx_id = ctx["tx_id"]
    field = ctx["field"]
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    tx = db.get_transaction(user_id, tx_id)
    if not tx:
        await state.clear()
        await message.answer("Giao dịch không tồn tại.")
        return

    if field == "amount":
        try:
            amount, _ = extract_amount_and_note(message.text)
            if amount <= 0:
                raise ValueError()
        except Exception:
            await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại.")
            return
        db.update_transaction_amount(user_id, tx_id, amount)
        msg = f"✅ Đã cập nhật *số tiền* giao dịch #{tx_id} thành `{amount:,.0f}`."
    elif field == "category":
        category = message.text.strip()
        if not category:
            await message.answer("❌ Danh mục không được để trống.")
            return
        db.update_transaction_category(user_id, tx_id, category)
        msg = f"✅ Đã cập nhật *danh mục* giao dịch #{tx_id} thành *{category}*."
    else:
        note = message.text.strip()
        if note == "-":
            note = ""
        db.update_transaction_note(user_id, tx_id, note)
        msg = f"✅ Đã cập nhật *ghi chú* giao dịch #{tx_id}."

    await state.clear()
    user_edit_tx_context.pop(message.from_user.id, None)
    await message.answer(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ---------- /categories – quản lý danh mục ----------


@router.message(Command("categories"))
async def cmd_categories(message: Message, state: FSMContext):
    await state.clear()
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    rows = db.get_categories(user_id)
    if not rows:
        db.ensure_default_categories(user_id)
        rows = db.get_categories(user_id)
    lines = ["📝 *Danh mục thu/chi của bạn:*\n"]
    for r in rows:
        icon = "➖" if r["type"] == "expense" else "➕"
        lines.append(f"{icon} {r['name']}")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Thêm danh mục", callback_data="cat_add"),
                InlineKeyboardButton(text="🗑 Xoá danh mục", callback_data="cat_delete_mode"),
            ]
        ]
    )
    await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data == "cat_add")
async def cb_cat_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(CategoryStates.choosing_type)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Thu nhập", callback_data="cat_type|income"),
                InlineKeyboardButton(text="➖ Chi tiêu", callback_data="cat_type|expense"),
            ]
        ]
    )
    await call.message.edit_text(
        "Chọn loại danh mục bạn muốn *thêm*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("cat_type"))
async def cb_cat_type(call: CallbackQuery, state: FSMContext):
    _, cat_type = call.data.split("|", maxsplit=1)
    await state.update_data(cat_type=cat_type)
    await state.set_state(CategoryStates.entering_name)
    label = "Thu nhập" if cat_type == "income" else "Chi tiêu"
    await call.message.edit_text(
        f"Nhập *tên danh mục* mới cho {label} (ví dụ: \"Freelance\", \"Đầu tư\", \"Con cái\"):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer()


@router.message(CategoryStates.entering_name)
async def cat_enter_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Tên danh mục không được để trống.")
        return
    data = await state.get_data()
    cat_type = data["cat_type"]
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.add_category(user_id, name, cat_type)
    await state.clear()
    await message.answer(
        f"✅ Đã thêm danh mục *{name}* ({'Thu nhập' if cat_type == 'income' else 'Chi tiêu'}).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "cat_delete_mode")
async def cb_cat_delete_mode(call: CallbackQuery):
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    rows = db.get_categories(user_id)
    if not rows:
        await call.answer("Bạn chưa có danh mục nào để xoá.", show_alert=True)
        return
    buttons = []
    for r in rows:
        icon = "➖" if r["type"] == "expense" else "➕"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {r['name']}",
                    callback_data=f"cat_delete|{r['id']}",
                )
            ]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text(
        "Chọn danh mục bạn muốn *xoá* (chỉ nên xoá nếu chắc chắn):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("cat_delete"))
async def cb_cat_delete(call: CallbackQuery):
    _, cat_id_str = call.data.split("|", maxsplit=1)
    cat_id = int(cat_id_str)
    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    db.delete_category(user_id, cat_id)
    await call.message.edit_text(
        "🗑 Đã xoá danh mục. Những giao dịch cũ vẫn giữ nguyên tên danh mục cũ.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer("Đã xoá danh mục.")


# ---------- /limit – đặt hạn mức chi tiêu ----------


@router.message(Command("limit"))
async def cmd_limit(message: Message, state: FSMContext):
    await state.set_state(LimitStates.choosing_category)
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    cats = db.get_categories(user_id, "expense")
    buttons = []
    row = []
    for c in cats:
        row.append(KeyboardButton(text=c["name"]))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)
    await message.answer(
        "⚙️ *Đặt hạn mức chi tiêu theo tháng*\n\n"
        "Chọn hoặc nhập *Danh mục chi tiêu* bạn muốn đặt hạn mức:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.message(LimitStates.choosing_category)
async def limit_choose_category(message: Message, state: FSMContext):
    category = message.text.strip()
    if not category:
        await message.answer("❌ Danh mục không được để trống.")
        return
    await state.update_data(category=category)
    await state.set_state(LimitStates.entering_amount)
    await message.answer(
        f"Nhập *hạn mức chi tiêu tháng* cho danh mục *{category}* "
        "(ví dụ: `2tr`, `2000000`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


@router.message(LimitStates.entering_amount)
async def limit_enter_amount(message: Message, state: FSMContext):
    raw = message.text
    try:
        amount, _ = extract_amount_and_note(raw)
        if amount <= 0:
            raise ValueError()
    except Exception:
        await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại.")
        return
    data = await state.get_data()
    category = data["category"]
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.set_limit(user_id, category, "month", amount)
    await state.clear()
    await message.answer(
        f"✅ Đã đặt hạn mức chi tiêu tháng cho *{category}*: `{amount:,.0f}`.\n"
        "Khi bạn ghi chi tiêu vượt hạn mức này, mình sẽ nhắc bạn ngay 😉",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ---------- /export – xuất CSV toàn bộ ----------


@router.message(Command("export"))
async def cmd_export(message: Message):
    """
    Xuất toàn bộ giao dịch của bạn ra file CSV.
    Có thể mở bằng Excel / Google Sheets.
    """
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    rows = db.get_all_transactions_for_export(user_id)

    if not rows:
        await message.answer(
            "📤 Bạn chưa có giao dịch nào để xuất.\n"
            "Hãy thêm vài giao dịch rồi thử lại nhé!",
            reply_markup=main_menu_kb(),
        )
        return

    lines = []
    header = "id,datetime_utc,type,amount,category,note,wallet"
    lines.append(header)

    for r in rows:
        tx_id = r["id"]
        dt = r["created_at"] or ""
        tx_type = r["type"] or ""
        amount = r["amount"] or 0
        category = r["category"] or ""
        note = r["note"] or ""
        wallet_name = r["wallet_name"] or ""

        def csv_escape(s: str) -> str:
            s = s.replace('"', '""')
            if ("," in s) or ("\n" in s) or ("\r" in s):
                return f'"{s}"'
            return s

        line = ",".join(
            [
                str(tx_id),
                csv_escape(dt),
                csv_escape(tx_type),
                str(int(amount)),
                csv_escape(category),
                csv_escape(note),
                csv_escape(wallet_name),
            ]
        )
        lines.append(line)

    csv_content = "\n".join(lines)
    csv_bytes = csv_content.encode("utf-8")

    buf = BufferedInputFile(
        csv_bytes,
        filename="transactions_export.csv",
    )

    await message.answer_document(
        document=buf,
        caption=(
            "📤 Đây là file *CSV* chứa toàn bộ giao dịch của bạn.\n"
            "Bạn có thể mở bằng *Excel*, *Google Sheets* hoặc bất kỳ ứng dụng bảng tính nào."
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ---------- /export_month – xuất CSV theo tháng ----------


@router.message(Command("export_month"))
async def cmd_export_month(message: Message, state: FSMContext):
    """
    Xuất giao dịch theo *một tháng cụ thể* ra file CSV.
    """
    db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await state.set_state(ExportMonthStates.entering_period)
    await message.answer(
        "📤 *Xuất CSV theo tháng*\n\n"
        "Nhập tháng bạn muốn xuất theo một trong các cách:\n"
        "• `03-2025` hoặc `3-2025`\n"
        "• `03/2025` hoặc `3/2025`\n"
        "• Hoặc gõ: `tháng này`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


@router.message(ExportMonthStates.entering_period)
async def export_month_enter_period(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()

    # 1) "tháng này"
    if "tháng này" in text or text.replace(" ", "") in ["thangnay", "thangnày"]:
        today = date.today()
        month = today.month
        year = today.year
    else:
        # 2) parse dạng MM-YYYY hoặc M/YYYY
        m = re.search(r"(\d{1,2})[^\d]+(\d{4})", text)
        if not m:
            await message.answer(
                "❌ Định dạng không hợp lệ.\n"
                "Vui lòng nhập lại, ví dụ: `03-2025`, `3/2025` hoặc `tháng này`.",
                reply_markup=main_menu_kb(),
            )
            return
        month = int(m.group(1))
        year = int(m.group(2))
        if month < 1 or month > 12:
            await message.answer("❌ Tháng phải từ 1 đến 12. Nhập lại giúp mình nhé.")
            return

    await state.clear()

    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    rows = db.get_transactions_for_month_export(user_id, year, month)

    if not rows:
        await message.answer(
            f"📤 Tháng {month:02d}/{year} không có giao dịch nào để xuất.",
            reply_markup=main_menu_kb(),
        )
        return

    lines = []
    header = "id,datetime_utc,type,amount,category,note,wallet"
    lines.append(header)

    for r in rows:
        tx_id = r["id"]
        dt = r["created_at"] or ""
        tx_type = r["type"] or ""
        amount = r["amount"] or 0
        category = r["category"] or ""
        note = r["note"] or ""
        wallet_name = r["wallet_name"] or ""

        def csv_escape(s: str) -> str:
            s = s.replace('"', '""')
            if ("," in s) or ("\n" in s) or ("\r" in s):
                return f'"{s}"'
            return s

        line = ",".join(
            [
                str(tx_id),
                csv_escape(dt),
                csv_escape(tx_type),
                str(int(amount)),
                csv_escape(category),
                csv_escape(note),
                csv_escape(wallet_name),
            ]
        )
        lines.append(line)

    csv_content = "\n".join(lines)
    csv_bytes = csv_content.encode("utf-8")

    filename = f"transactions_{year}_{month:02d}.csv"
    buf = BufferedInputFile(
        csv_bytes,
        filename=filename,
    )

    await message.answer_document(
        document=buf,
        caption=(
            f"📤 Đây là file *CSV* giao dịch tháng {month:02d}/{year}.\n"
            "Bạn có thể mở bằng *Excel*, *Google Sheets* hoặc bất kỳ ứng dụng bảng tính nào."
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ---------- /export_wallet – xuất CSV theo từng ví ----------


@router.message(Command("export_wallet"))
async def cmd_export_wallet(message: Message):
    """
    Xuất giao dịch theo *từng ví* ra file CSV.
    Bước 1: cho user chọn ví bằng inline keyboard.
    """
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.ensure_default_wallets(user_id)
    wallets = db.get_wallets(user_id)

    if not wallets:
        await message.answer(
            "📤 Hiện bạn chưa có ví nào để xuất.\n"
            "Thử ghi lương bằng /salary hoặc thêm giao dịch trước nhé.",
            reply_markup=main_menu_kb(),
        )
        return

    buttons = []
    for w in wallets:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{w['name']}",
                    callback_data=f"export_wallet|{w['id']}",
                )
            ]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        "📤 *Xuất CSV theo từng ví*\n\n"
        "Chọn *ví* bạn muốn xuất dữ liệu:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("export_wallet|"))
async def cb_export_wallet(call: CallbackQuery):
    """
    Khi user bấm chọn 1 ví, bot sẽ tạo CSV cho riêng ví đó và gửi file.
    """
    _, wallet_id_str = call.data.split("|", maxsplit=1)
    try:
        wallet_id = int(wallet_id_str)
    except ValueError:
        await call.answer("Dữ liệu ví không hợp lệ.", show_alert=True)
        return

    user_id = db.get_or_create_user(call.from_user.id, call.from_user.full_name)
    wallet = db.get_wallet(user_id, wallet_id)
    if not wallet:
        await call.answer("Không tìm thấy ví này.", show_alert=True)
        return

    rows = db.get_transactions_for_wallet_export(user_id, wallet_id)

    if not rows:
        await call.message.edit_text(
            f"📤 Ví *{wallet['name']}* hiện chưa có giao dịch nào để xuất.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await call.answer()
        return

    lines = []
    header = "id,datetime_utc,type,amount,category,note,wallet"
    lines.append(header)

    for r in rows:
        tx_id = r["id"]
        dt = r["created_at"] or ""
        tx_type = r["type"] or ""
        amount = r["amount"] or 0
        category = r["category"] or ""
        note = r["note"] or ""
        wallet_name = r["wallet_name"] or ""

        def csv_escape(s: str) -> str:
            s = s.replace('"', '""')
            if ("," in s) or ("\n" in s) or ("\r" in s):
                return f'"{s}"'
            return s

        line = ",".join(
            [
                str(tx_id),
                csv_escape(dt),
                csv_escape(tx_type),
                str(int(amount)),
                csv_escape(category),
                csv_escape(note),
                csv_escape(wallet_name),
            ]
        )
        lines.append(line)

    csv_content = "\n".join(lines)
    csv_bytes = csv_content.encode("utf-8")

    filename = f"transactions_wallet_{wallet_id}.csv"
    buf = BufferedInputFile(
        csv_bytes,
        filename=filename,
    )

    await call.message.edit_text(
        f"📤 Đang gửi file CSV cho ví *{wallet['name']}*...",
        parse_mode=ParseMode.MARKDOWN,
    )

    await call.message.answer_document(
        document=buf,
        caption=(
            f"📤 Đây là file *CSV* giao dịch của ví *{wallet['name']}*.\n"
            "Bạn có thể mở bằng *Excel*, *Google Sheets* hoặc bất kỳ ứng dụng bảng tính nào."
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )
    await call.answer()


# ---------- /backup – sao lưu file DB ----------


@router.message(Command("backup"))
async def cmd_backup(message: Message):
    """
    Gửi cho bạn file database SQLite hiện tại để tự backup.
    LƯU Ý: file này chứa dữ liệu của tất cả user đang dùng bot.
    """
    if not os.path.exists(DB_PATH):
        await message.answer(
            "❌ Không tìm thấy file database để backup.",
            reply_markup=main_menu_kb(),
        )
        return

    with open(DB_PATH, "rb") as f:
        data = f.read()

    buf = BufferedInputFile(
        data,
        filename=os.path.basename(DB_PATH),
    )

    await message.answer_document(
        document=buf,
        caption=(
            "📦 Đây là file *database* hiện tại của bot.\n"
            "Bạn hãy lưu trữ cẩn thận (Drive, cloud, USB,...) để backup."
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# ---------- FALLBACK ----------


@router.message()
async def fallback_handler(message: Message):
    txt = (message.text or "").strip()
    if any(ch.isdigit() for ch in txt):
        await message.answer(
            "Mình đoán bạn đang muốn ghi thu/chi 🤔\n\n"
            "Bạn có thể:\n"
            "• Bấm *➕ Thu nhập* hoặc *➖ Chi tiêu* trên menu\n"
            "• Hoặc dùng lệnh /add rồi nhập kiểu: `35k ăn sáng`, `1.2tr tiền nhà`.\n\n"
            "Gõ /help để xem hướng dẫn chi tiết.",
            reply_markup=main_menu_kb(),
        )
    else:
        await message.answer(
            "Mình chưa hiểu yêu cầu của bạn 🥲\n\n"
            "Dùng menu phía dưới hoặc gõ /help để xem hướng dẫn.",
            reply_markup=main_menu_kb(),
        )


# ==========================
# WEBHOOK + AIOHTTP
# ==========================


async def on_startup(bot: Bot):
    if not WEBHOOK_URL:
        logging.error("BASE_WEBHOOK_URL chưa được cấu hình, không thể đặt webhook.")
        return
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Đã đặt webhook: {WEBHOOK_URL}")


def create_app() -> web.Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN chưa được thiết lập trong biến môi trường.")

    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    app = web.Application()
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    async def health(request: web.Request):
        return web.Response(text="OK - finance bot PRO multi-wallet is running")

    app.router.add_get("/", health)
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
