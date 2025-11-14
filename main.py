# main.py
# Bot Telegram quản lý tài chính cá nhân theo luật 4-2-2-2
# Sử dụng: aiogram (v3), aiohttp (webhook), SQLite để lưu dữ liệu
# Mọi message / comment: tiếng Việt

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, date

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
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ==========================
# CẤU HÌNH CƠ BẢN
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")  # Token lấy từ BotFather
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")  # Ví dụ: https://your-app.onrender.com
WEBHOOK_PATH = "/telegram-webhook"  # path cố định cho webhook
WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}" if BASE_WEBHOOK_URL else None

PORT = int(os.getenv("PORT", "8080"))  # Render cung cấp PORT qua env
DB_PATH = os.getenv("DB_PATH", "finance_bot.db")  # file SQLite


# ==========================
# LỚP QUẢN LÝ DATABASE (SQLite)
# ==========================

class Database:
    """
    Lớp quản lý SQLite đơn giản.
    Dùng đồng bộ (blocking) nhưng đủ cho bot nhỏ miễn phí.
    """

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()

        # Bảng user
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                full_name TEXT,
                created_at TEXT
            )
            """
        )

        # Bảng giao dịch (income/expense)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT, -- 'income' hoặc 'expense'
                amount REAL,
                category TEXT,
                note TEXT,
                created_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )

        # Bảng mục tiêu tiết kiệm
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
            )
            """
        )

        # Bảng lịch sử nạp/rút vào mục tiêu
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
            )
            """
        )

        # Bảng ghi chú ngân sách 4-2-2-2
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
            )
            """
        )

        self.conn.commit()

    # ---------- User ----------

    def get_or_create_user(self, telegram_id: int, full_name: str | None) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        if row:
            return row["id"]
        now = datetime.utcnow().isoformat()
        cur.execute(
            "INSERT INTO users (telegram_id, full_name, created_at) VALUES (?, ?, ?)",
            (telegram_id, full_name or "", now),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_user_id(self, telegram_id: int) -> int | None:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        return row["id"] if row else None

    # ---------- Giao dịch ----------

    def add_transaction(
        self,
        user_id: int,
        tx_type: str,
        amount: float,
        category: str,
        note: str,
    ):
        now = datetime.utcnow().isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO transactions (user_id, type, amount, category, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, tx_type, amount, category, note, now),
        )
        self.conn.commit()

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

    def get_summary(
        self, user_id: int, start: datetime, end: datetime
    ) -> dict:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT type, COALESCE(SUM(amount),0) AS total
            FROM transactions
            WHERE user_id = ?
              AND created_at BETWEEN ? AND ?
            GROUP BY type
            """,
            (user_id, start.isoformat(), end.isoformat()),
        )
        data = {"income": 0.0, "expense": 0.0}
        for row in cur.fetchall():
            data[row["type"]] = row["total"]
        return data

    def get_category_summary_month(self, user_id: int, year: int, month: int):
        # Thống kê theo danh mục trong tháng (cho chi tiêu)
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
            WHERE user_id = ?
              AND type = 'expense'
              AND created_at BETWEEN ? AND ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (user_id, first.isoformat(), last.isoformat()),
        )
        return cur.fetchall()

    # ---------- Mục tiêu tiết kiệm ----------

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
            WHERE user_id = ?
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
            WHERE id = ?
            """,
            (goal_id,),
        )
        return cur.fetchone()

    def update_goal_amount(self, goal_id: int, new_amount: float):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE saving_goals SET current_amount = ? WHERE id = ?",
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

    # ---------- Ngân sách 4-2-2-2 ----------

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


db = Database(DB_PATH)

# ==========================
# TRẠNG THÁI FSM
# ==========================

class AddTransactionStates(StatesGroup):
    choosing_type = State()
    entering_amount = State()
    entering_category = State()
    entering_note = State()


class CreateGoalStates(StatesGroup):
    entering_name = State()
    entering_target = State()


class GoalMoneyStates(StatesGroup):
    choosing_action = State()  # không dùng nhiều, nhưng để mở rộng
    entering_amount = State()
    entering_note = State()


# Lưu tạm goal_id cho nạp / rút
user_goal_action_context: dict[int, dict] = {}


# ==========================
# KEYBOARD HỖ TRỢ
# ==========================

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Ghi giao dịch"),
                KeyboardButton(text="📊 Xem báo cáo"),
            ],
            [
                KeyboardButton(text="🎯 Mục tiêu tiết kiệm"),
                KeyboardButton(text="📐 Ngân sách 4-2-2-2"),
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
    # callback_data: budget_note|<total>, budget_goals|<total>
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Lưu thành ghi chú ngân sách",
                    callback_data=f"budget_note|{total_income}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Tạo mục tiêu tiết kiệm (2 khoản 20%)",
                    callback_data=f"budget_goals|{total_income}",
                )
            ],
        ]
    )


def goals_inline_kb(goals_rows) -> InlineKeyboardMarkup:
    # tạo inline keyboard với mỗi goal có 2 nút: Gửi tiền / Rút tiền
    rows = []
    for row in goals_rows:
        goal_id = row["id"]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"➕ Gửi tiền: #{goal_id}",
                    callback_data=f"goal_deposit|{goal_id}",
                ),
                InlineKeyboardButton(
                    text=f"➖ Rút tiền: #{goal_id}",
                    callback_data=f"goal_withdraw|{goal_id}",
                ),
            ]
        )
    # Thêm nút tạo mới
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Tạo mục tiêu mới", callback_data="goal_create_new"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_menu_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📆 Hôm nay", callback_data="report_today"),
                InlineKeyboardButton(text="📅 7 ngày qua", callback_data="report_7days"),
            ],
            [
                InlineKeyboardButton(text="🗓 Tháng này", callback_data="report_month"),
                InlineKeyboardButton(text="📊 Theo danh mục (tháng)", callback_data="report_categories"),
            ],
            [
                InlineKeyboardButton(text="💼 Số dư hiện tại", callback_data="report_balance"),
            ],
        ]
    )


# ==========================
# ROUTER & HANDLERS
# ==========================

router = Router()


# ---------- Lệnh /start ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await state.clear()
    text = (
        "Xin chào, "
        f"*{message.from_user.full_name}* 👋\n\n"
        "Mình là bot quản lý tài chính cá nhân của bạn.\n\n"
        "Bạn có thể:\n"
        "• Ghi lại *Thu nhập / Chi tiêu*\n"
        "• Tạo & theo dõi *Mục tiêu tiết kiệm*\n"
        "• Xem *báo cáo* theo ngày/tuần/tháng\n"
        "• Tính *ngân sách 4-2-2-2* từ lương của bạn\n\n"
        "Gõ /help để xem chi tiết lệnh.\n"
        "Hoặc dùng menu bên dưới cho nhanh nhé 👇"
    )
    await message.answer(
        text, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN
    )


# ---------- Lệnh /help ----------

@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "🆘 *Hướng dẫn sử dụng bot*\n\n"
        "Các lệnh chính:\n"
        "• /start – Bắt đầu, hiển thị menu chính\n"
        "• /help – Xem hướng dẫn\n"
        "• /add – Ghi giao dịch Thu nhập hoặc Chi tiêu\n"
        "• /report – Xem báo cáo và số dư\n"
        "• /goals – Quản lý mục tiêu tiết kiệm\n"
        "• /budget – Tính ngân sách theo quy tắc 4-2-2-2\n\n"
        "Bạn cũng có thể dùng các nút trên bàn phím (Reply Keyboard) để thao tác nhanh."
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


# ---------- Trigger từ Reply Keyboard ----------

@router.message(F.text == "➕ Ghi giao dịch")
async def handle_add_btn(message: Message, state: FSMContext):
    await cmd_add(message, state)


@router.message(F.text == "📊 Xem báo cáo")
async def handle_report_btn(message: Message):
    await cmd_report(message)


@router.message(F.text == "🎯 Mục tiêu tiết kiệm")
async def handle_goals_btn(message: Message, state: FSMContext):
    await cmd_goals(message, state)


@router.message(F.text == "📐 Ngân sách 4-2-2-2")
async def handle_budget_btn(message: Message, state: FSMContext):
    await cmd_budget(message, state)


# ---------- /add – Ghi giao dịch (FSM) ----------

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    await state.set_state(AddTransactionStates.choosing_type)
    text = (
        "Bạn muốn ghi *Thu nhập* hay *Chi tiêu*?\n\n"
        "Chọn bằng các nút bên dưới:"
    )
    await message.answer(
        text, reply_markup=income_expense_inline_kb(), parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data.startswith("add_tx_type"))
async def cb_add_tx_type(call: CallbackQuery, state: FSMContext):
    _, tx_type = call.data.split("|", maxsplit=1)
    await state.update_data(tx_type=tx_type)
    await state.set_state(AddTransactionStates.entering_amount)
    await call.message.edit_text(
        "Vui lòng nhập *số tiền* (chỉ số, ví dụ: `150000`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer()


@router.message(AddTransactionStates.entering_amount)
async def add_tx_amount(message: Message, state: FSMContext):
    text = message.text.replace(",", "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại (ví dụ: 200000).")
        return

    await state.update_data(amount=amount)
    await state.set_state(AddTransactionStates.entering_category)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Ăn uống"), KeyboardButton(text="Đi lại")],
            [KeyboardButton(text="Nhà cửa"), KeyboardButton(text="Lương")],
            [KeyboardButton(text="Khác")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await message.answer(
        "Nhập *danh mục* cho giao dịch (hoặc chọn gợi ý trên bàn phím):",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(AddTransactionStates.entering_category)
async def add_tx_category(message: Message, state: FSMContext):
    category = message.text.strip()
    if not category:
        await message.answer("❌ Danh mục không được để trống, vui lòng nhập lại.")
        return

    await state.update_data(category=category)
    await state.set_state(AddTransactionStates.entering_note)
    await message.answer(
        "Nhập *ghi chú* cho giao dịch (hoặc gõ `-` nếu không có):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


@router.message(AddTransactionStates.entering_note)
async def add_tx_note(message: Message, state: FSMContext):
    note = message.text.strip()
    if note == "-":
        note = ""

    data = await state.get_data()
    tx_type = data.get("tx_type")
    amount = data.get("amount")
    category = data.get("category")

    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.add_transaction(user_id, tx_type, amount, category, note)

    await state.clear()

    label = "Thu nhập" if tx_type == "income" else "Chi tiêu"
    sign = "+" if tx_type == "income" else "-"
    text = (
        "✅ Đã ghi giao dịch:\n\n"
        f"• Loại: *{label}*\n"
        f"• Số tiền: `{amount:,.0f}`\n"
        f"• Danh mục: *{category}*\n"
        f"• Ghi chú: {note or 'Không có'}\n\n"
        f"{sign}`{amount:,.0f}` đã được cập nhật vào sổ giao dịch của bạn."
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ---------- /budget – Quy tắc 4-2-2-2 ----------

@router.message(Command("budget"))
async def cmd_budget(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "💰 *Tính ngân sách 4-2-2-2*\n\n"
        "Vui lòng nhập *tổng lương / thu nhập hàng tháng* của bạn "
        "(ví dụ: `15000000`):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(F.text.regexp(r"^\d+(\.\d+)?$"))
async def handle_budget_amount_if_in_budget(message: Message):
    """
    Để đơn giản, nếu người dùng vừa gõ số ngay sau /budget,
    ta hiểu là số lương nhập để tính 4-2-2-2.
    (Đồng thời người dùng có thể nhập số ở chỗ khác, nhưng chấp nhận được cho bot cá nhân.)
    """
    # Chỉ kích hoạt nếu vừa gọi /budget hoặc người dùng chủ động nhập số.
    # Để tránh đụng FSM khác, ta kiểm tra text và tiếp tục.
    total_income = float(message.text.replace(",", ""))
    if total_income <= 0:
        await message.reply("❌ Số tiền không hợp lệ, vui lòng nhập số dương.")
        return

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
        "Bạn có muốn *tự động lưu* các khoản này không?"
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
        total_income = float(total_str)
    except ValueError:
        await call.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return

    essential = total_income * 0.4
    long_term = total_income * 0.2
    invest = total_income * 0.2
    personal = total_income * 0.2

    user_id = db.get_or_create_user(
        call.from_user.id, call.from_user.full_name
    )
    db.save_budget(
        user_id,
        total_income,
        essential,
        long_term,
        invest,
        personal,
    )

    await call.message.edit_text(
        "✅ Đã lưu *ghi chú ngân sách 4-2-2-2* cho bạn.\n\n"
        "Bạn có thể tính lại /budget bất cứ lúc nào.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer("Đã lưu ghi chú ngân sách.")


@router.callback_query(F.data.startswith("budget_goals"))
async def cb_budget_goals(call: CallbackQuery):
    _, total_str = call.data.split("|", maxsplit=1)
    try:
        total_income = float(total_str)
    except ValueError:
        await call.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return

    long_term = total_income * 0.2
    invest = total_income * 0.2

    user_id = db.get_or_create_user(
        call.from_user.id, call.from_user.full_name
    )

    # Tạo 2 mục tiêu tiết kiệm tương ứng hai khoản 20%
    db.create_saving_goal(user_id, "Tiết kiệm dài hạn (4-2-2-2)", long_term)
    db.create_saving_goal(user_id, "Đầu tư & Tự do tài chính (4-2-2-2)", invest)

    await call.message.edit_text(
        "✅ Đã tạo 2 *Mục tiêu tiết kiệm* dựa trên 20% Tiết kiệm dài hạn "
        "và 20% Đầu tư & Tự do tài chính.\n\n"
        "Bạn có thể xem tại /goals.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer("Đã tạo mục tiêu tiết kiệm từ ngân sách 4-2-2-2.")


# ---------- /report – Báo cáo ----------

@router.message(Command("report"))
async def cmd_report(message: Message):
    db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    text = (
        "📊 *Báo cáo tài chính*\n\n"
        "Chọn loại báo cáo bạn muốn xem:"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=report_menu_inline_kb())


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
    if today.month == 12:
        end = datetime(today.year + 1, 1, 1)
    else:
        end = datetime(today.year, today.month + 1, 1)
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
        text = "📊 *Thống kê theo danh mục (tháng này)*\n\nChưa có chi tiêu nào được ghi lại."
    else:
        lines = [
            "📊 *Thống kê chi tiêu theo danh mục (tháng này)*\n"
        ]
        for row in rows:
            lines.append(f"• {row['category']}: `{row['total']:,.0f}`")
        text = "\n".join(lines)

    await call.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=report_menu_inline_kb())
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


# ---------- /goals – Mục tiêu tiết kiệm ----------

@router.message(Command("goals"))
async def cmd_goals(message: Message, state: FSMContext):
    await state.clear()
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    goals = db.get_saving_goals(user_id)

    if not goals:
        text = (
            "🎯 *Mục tiêu tiết kiệm*\n\n"
            "Hiện bạn chưa có mục tiêu nào.\n"
            "Gõ /goals_add để tạo mới hoặc bấm nút bên dưới."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Tạo mục tiêu mới", callback_data="goal_create_new")]
            ]
        )
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
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

    text = "\n".join(lines)
    await message.answer(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=goals_inline_kb(goals),
    )


# /goals_add – tạo mục tiêu (cũng có thể được gọi từ callback goal_create_new)

@router.message(Command("goals_add"))
async def cmd_goals_add(message: Message, state: FSMContext):
    await state.set_state(CreateGoalStates.entering_name)
    await message.answer(
        "🎯 Tạo *Mục tiêu tiết kiệm* mới\n\n"
        "Bước 1: Nhập *tên mục tiêu* (ví dụ: \"Quỹ khẩn cấp\", \"Du lịch Nhật Bản\").",
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
        "Bước 2: Nhập *số tiền cần đạt* cho mục tiêu (ví dụ: `50000000`):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(CreateGoalStates.entering_target)
async def goal_enter_target(message: Message, state: FSMContext):
    text = message.text.replace(",", "").strip()
    try:
        target = float(text)
        if target <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại (ví dụ: 50000000).")
        return

    data = await state.get_data()
    name = data.get("goal_name")

    user_id = db.get_or_create_user(message.from_user.id, message.from_user.full_name)
    db.create_saving_goal(user_id, name, target)
    await state.clear()

    await message.answer(
        f"✅ Đã tạo mục tiêu *{name}* với số tiền cần đạt `{target:,.0f}`.\n"
        "Dùng /goals để xem danh sách và nạp / rút tiền.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


# --- Nạp / rút tiền mục tiêu qua inline keyboard ---

@router.callback_query(F.data.startswith("goal_deposit"))
async def cb_goal_deposit(call: CallbackQuery, state: FSMContext):
    _, goal_id_str = call.data.split("|", maxsplit=1)
    goal_id = int(goal_id_str)
    goal = db.get_goal(goal_id)
    if not goal:
        await call.answer("Mục tiêu không tồn tại.", show_alert=True)
        return

    # Lưu context tạm
    user_goal_action_context[call.from_user.id] = {
        "goal_id": goal_id,
        "action": "deposit",
        "goal_name": goal["name"],
    }

    await state.set_state(GoalMoneyStates.entering_amount)
    await call.message.edit_text(
        f"➕ *Gửi tiền* vào mục tiêu *{goal['name']}*\n\n"
        "Nhập số tiền muốn gửi (ví dụ: `1000000`):",
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
        "goal_name": goal["name"],
    }

    await state.set_state(GoalMoneyStates.entering_amount)
    await call.message.edit_text(
        f"➖ *Rút tiền* từ mục tiêu *{goal['name']}*\n\n"
        "Nhập số tiền muốn rút (ví dụ: `500000`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    await call.answer()


@router.message(GoalMoneyStates.entering_amount)
async def goal_money_amount(message: Message, state: FSMContext):
    ctx = user_goal_action_context.get(message.from_user.id)
    if not ctx:
        await state.clear()
        await message.answer("Phiên thao tác mục tiêu đã hết hạn. Vui lòng mở lại /goals.")
        return

    text = message.text.replace(",", "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Số tiền không hợp lệ, vui lòng nhập lại.")
        return

    await state.update_data(amount=amount)
    await state.set_state(GoalMoneyStates.entering_note)
    await message.answer(
        "Nhập ghi chú cho lần gửi/rút này (hoặc gõ `-` nếu không có):",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(GoalMoneyStates.entering_note)
async def goal_money_note(message: Message, state: FSMContext):
    ctx = user_goal_action_context.get(message.from_user.id)
    if not ctx:
        await state.clear()
        await message.answer("Phiên thao tác mục tiêu đã hết hạn. Vui lòng mở lại /goals.")
        return

    data = await state.get_data()
    amount = data.get("amount")
    if amount is None:
        await message.answer("Có lỗi xảy ra, vui lòng thử lại.")
        await state.clear()
        return

    note = message.text.strip()
    if note == "-":
        note = ""

    goal = db.get_goal(ctx["goal_id"])
    if not goal:
        await message.answer("Mục tiêu không tồn tại nữa.")
        await state.clear()
        return

    action = ctx["action"]
    current = goal["current_amount"]

    if action == "deposit":
        new_amount = current + amount
        db.update_goal_amount(goal["id"], new_amount)
        db.add_goal_transaction(goal["id"], "deposit", amount, note)
        text = (
            f"✅ Đã *gửi* `{amount:,.0f}` vào mục tiêu *{goal['name']}*.\n"
            f"Số tiền hiện tại: `{new_amount:,.0f} / {goal['target_amount']:,.0f}`"
        )
    else:
        if amount > current:
            await message.answer(
                f"❌ Bạn chỉ có thể rút tối đa `{current:,.0f}` (số tiền hiện có trong mục tiêu)."
            )
            return
        new_amount = current - amount
        db.update_goal_amount(goal["id"], new_amount)
        db.add_goal_transaction(goal["id"], "withdraw", amount, note)
        text = (
            f"✅ Đã *rút* `{amount:,.0f}` từ mục tiêu *{goal['name']}*.\n"
            f"Số tiền còn lại: `{new_amount:,.0f} / {goal['target_amount']:,.0f}`"
        )

    await state.clear()
    user_goal_action_context.pop(message.from_user.id, None)

    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())


# ---------- Fallback: nếu user gõ số mà không phải trong /budget hay FSM ----------

@router.message()
async def fallback_handler(message: Message):
    # Nếu không khớp handler nào, chỉ hướng dẫn nhẹ nhàng
    if message.text and message.text.strip().isdigit():
        await message.answer(
            "Mình không chắc bạn muốn làm gì với con số này 🤔\n"
            "Bạn có thể:\n"
            "• Dùng /add để ghi giao dịch\n"
            "• Dùng /budget rồi nhập số lương để tính 4-2-2-2\n"
            "• Dùng /report hoặc /goals để xem thông tin hiện có.",
            reply_markup=main_menu_kb(),
        )
    else:
        await message.answer(
            "Mình chưa hiểu yêu cầu của bạn 🥲\n\n"
            "Gõ /help để xem danh sách lệnh, hoặc dùng menu bên dưới nhé.",
            reply_markup=main_menu_kb(),
        )


# ==========================
# WEBHOOK + AIOHTTP SERVER
# ==========================

async def on_startup(bot: Bot):
    """
    Hàm chạy khi Dispatcher khởi động.
    Đặt webhook để Telegram gửi update tới URL HTTPS cố định.
    """
    if not WEBHOOK_URL:
        logging.error("BASE_WEBHOOK_URL chưa được cấu hình. Không thể đặt webhook.")
        return

    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Đã đặt webhook: {WEBHOOK_URL}")


def create_app() -> web.Application:
    """
    Tạo aiohttp Application, gắn handler webhook của aiogram vào path WEBHOOK_PATH.
    """
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN chưa được thiết lập trong biến môi trường.")

    if not BASE_WEBHOOK_URL:
        logging.warning(
            "BASE_WEBHOOK_URL chưa được thiết lập. "
            "Hãy set biến này trên môi trường production (ví dụ Render)."
        )

    # Tạo Dispatcher & Bot
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    app = web.Application()

    # Handler webhook đơn giản
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)

    # Gắn lifecycle của Dispatcher vào app
    setup_application(app, dp, bot=bot)

    # Endpoint đơn giản để health-check
    async def health(request: web.Request):
        return web.Response(text="OK - finance bot is running")

    app.router.add_get("/", health)

    return app


def main():
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    # Lắng nghe trên 0.0.0.0:PORT (Render sẽ reverse proxy HTTPS vào đây)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()