import asyncio
import glob
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List, Set
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from dotenv import load_dotenv

# ================= НАСТРОЙКИ =================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env файле!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
NOTIFICATION_CHAT_ID = int(os.getenv("NOTIFICATION_CHAT_ID", ADMIN_ID))

WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "10"))
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", "20"))
BOOKING_DAYS_AHEAD = int(os.getenv("BOOKING_DAYS_AHEAD", "14"))
MAX_ACTIVE_BOOKINGS = int(os.getenv("MAX_ACTIVE_BOOKINGS", "3"))
PORTFOLIO_DIR = os.getenv("PORTFOLIO_DIR", "portfolio")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================= ДАННЫЕ =================
@dataclass
class Service:
    name: str
    description: str
    price: str
    duration: int

PRICES = [
    Service("💅 Маникюр", "Классический / аппаратный", "1500 ₽", 60),
    Service("🎨 Покрытие гель-лак", "Однотонное, дизайн по желанию", "1000 ₽", 45),
    Service("✨ Наращивание", "Гель, форма на выбор", "2500 ₽", 90),
    Service("🦶 Педикюр", "Аппаратный + покрытие", "2200 ₽", 80),
    Service("💎 Дизайн", "Френч, стразы, слайдеры", "от 200 ₽", 30),
    Service("🧴 Снятие + уход", "Снятие покрытия, масло, крем", "300 ₽", 20),
]

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= КНОПКИ ВНИЗУ (REPLY KEYBOARD) =================
def main_menu_kb():
    """Главное меню - кнопки внизу экрана"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Записаться"), KeyboardButton(text="💰 Прайс-лист")],
            [KeyboardButton(text="📸 Примеры работ"), KeyboardButton(text="⭐ Отзывы")],
            [KeyboardButton(text="👩‍🎨 О мастере"), KeyboardButton(text="💬 Оставить отзыв")],
            [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="❌ Отменить запись")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )

def admin_menu_kb():
    """Админ-панель - кнопки внизу экрана"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Все записи"), KeyboardButton(text="⭐ Отзывы на модерации")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⬅️ В главное меню")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Админ-панель..."
    )

def cancel_kb():
    """Кнопка отмены"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Отменить действие")],
        ],
        resize_keyboard=True
    )

# ================= БАЗА ДАННЫХ =================
class Database:
    def __init__(self, db_name: str = "bot.db"):
        self.db_name = db_name
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Ошибка БД: {e}")
            raise
        finally:
            conn.close()
    
    def init_db(self):
        with self.get_connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tg_username TEXT,
                day TEXT,
                time TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                email TEXT,
                services TEXT,
                comment TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_at TEXT,
                updated_at TEXT
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                rating INTEGER,
                text TEXT,
                is_approved INTEGER DEFAULT 0,
                created_at TEXT
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                tg_username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                email TEXT,
                created_at TEXT,
                last_activity TEXT
            )""")
            
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_day ON bookings(day)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)")
    
    def get_active_booking_count(self, user_id: int) -> int:
        with self.get_connection() as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND status = 'active' AND day >= date('now')",
                (user_id,)
            ).fetchone()
            return result[0] if result else 0
    
    def can_make_booking(self, user_id: int) -> tuple[bool, str]:
        active_count = self.get_active_booking_count(user_id)
        if active_count >= MAX_ACTIVE_BOOKINGS:
            return False, f"❌ У Вас уже есть {MAX_ACTIVE_BOOKINGS} активных записей.\nОтмените одну, чтобы создать новую."
        return True, "✅ Можно создать запись"
    
    def save_booking(self, user_id: int, tg_username: str, day: str, time: str,
                     first_name: str, last_name: str, phone: str, email: str,
                     services: List[str], comment: str = "") -> tuple[bool, str]:
        can_book, message = self.can_make_booking(user_id)
        if not can_book:
            return False, message
        
        with self.get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM bookings WHERE day = ? AND time = ? AND status = 'active'",
                (day, time)
            ).fetchone()
            if existing:
                return False, "❌ Это время уже занято."
            
            services_str = ", ".join(services)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""INSERT INTO bookings 
                (user_id, tg_username, day, time, first_name, last_name, phone, email, services, comment, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, tg_username, day, time, first_name, last_name, phone, email, services_str, comment, 'active', now, now))
            
            self.update_user(user_id, tg_username, first_name, last_name, phone, email)
            return True, "✅ Запись создана!"
    
    def cancel_booking(self, booking_id: int, user_id: int) -> tuple[bool, str]:
        with self.get_connection() as conn:
            booking = conn.execute(
                "SELECT * FROM bookings WHERE id = ? AND user_id = ? AND status = 'active'",
                (booking_id, user_id)
            ).fetchone()
            if not booking:
                return False, "❌ Запись не найдена"
            
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE bookings SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, booking_id)
            )
            return True, "✅ Запись отменена"
    
    def get_user_bookings(self, user_id: int) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, day, time, first_name, last_name, phone, email, services, comment, created_at
                FROM bookings 
                WHERE user_id = ? AND status = 'active' AND day >= date('now')
                ORDER BY day, time
            """, (user_id,)).fetchall()
            return [dict(row) for row in rows]
    
    def get_all_bookings(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, user_id, tg_username, day, time, first_name, last_name, phone, email, services, comment, created_at
                FROM bookings 
                WHERE status = 'active' AND day >= date('now')
                ORDER BY day, time
            """).fetchall()
            return [dict(row) for row in rows]
    
    def get_booked_slots(self, day: str) -> set:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT time FROM bookings WHERE day = ? AND status = 'active'",
                (day,)
            ).fetchall()
            return {row[0] for row in rows}
    
    def update_user(self, user_id: int, tg_username: str, first_name: str, last_name: str, phone: str, email: str):
        with self.get_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT OR REPLACE INTO users (user_id, tg_username, first_name, last_name, phone, email, last_activity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, tg_username, first_name, last_name, phone, email, now))
    
    def save_feedback(self, user_id: int, username: str, rating: int, text: str):
        with self.get_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""INSERT INTO feedback 
                (user_id, username, rating, text, is_approved, created_at)
                VALUES (?,?,?,?,0,?)""",
                (user_id, username, rating, text, now))
    
    def get_approved_feedback(self, limit: int = 10) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT username, rating, text, created_at
                FROM feedback 
                WHERE is_approved = 1
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]
    
    def get_unapproved_feedback(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, user_id, username, rating, text, created_at
                FROM feedback 
                WHERE is_approved = 0
                ORDER BY created_at DESC
            """).fetchall()
            return [dict(row) for row in rows]
    
    def approve_feedback(self, feedback_id: int):
        with self.get_connection() as conn:
            conn.execute("UPDATE feedback SET is_approved = 1 WHERE id = ?", (feedback_id,))

db = Database()

# ================= INLINE КЛАВИАТУРЫ (для кнопок в сообщениях) =================
def inline_menu_kb():
    buttons = [
        [InlineKeyboardButton(text="📅 Записаться", callback_data="book")],
        [InlineKeyboardButton(text="💰 Прайс-лист", callback_data="price")],
        [InlineKeyboardButton(text="📸 Примеры работ", callback_data="portfolio")],
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="reviews")],
        [InlineKeyboardButton(text="👩‍🎨 О мастере", callback_data="about")],
        [InlineKeyboardButton(text="💬 Оставить отзыв", callback_data="feedback")],
        [InlineKeyboardButton(text="📋 Мои записи", callback_data="my_bookings")],
    ]
    if ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]])

def days_inline_kb():
    buttons = []
    row = []
    today = date.today()
    for i in range(1, BOOKING_DAYS_AHEAD + 1):
        d = today + timedelta(days=i)
        if d == today and datetime.now().hour >= WORK_END_HOUR:
            continue
        label = f"{WEEKDAYS[d.weekday()]} {d.day}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"day_{d.isoformat()}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def times_inline_kb(day: str):
    buttons = []
    row = []
    taken = db.get_booked_slots(day)
    for hour in range(WORK_START_HOUR, WORK_END_HOUR):
        t = f"{hour:02d}:00"
        if t not in taken:
            if day == date.today().isoformat() and hour <= datetime.now().hour:
                continue
            row.append(InlineKeyboardButton(text=t, callback_data=f"time_{day}_{t}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="book")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def services_inline_kb(selected: List[str] = None):
    if selected is None:
        selected = []
    buttons = []
    row = []
    for i, service in enumerate(PRICES):
        check = "✅ " if service.name in selected else ""
        row.append(InlineKeyboardButton(
            text=f"{check}{service.name[:15]}",
            callback_data=f"svc_{i}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    buttons.append([
        InlineKeyboardButton(text="✅ Готово", callback_data="services_done"),
        InlineKeyboardButton(text="❌ Очистить все", callback_data="services_clear")
    ])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="book")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_feedback_inline_kb(feedback_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"admin_approve_fb_{feedback_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_fb_{feedback_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_feedback")]
    ])

# ================= FSM =================
class BookingStates(StatesGroup):
    first_name = State()
    last_name = State()
    phone = State()
    email = State()
    services = State()
    comment = State()
    confirm = State()
    cancel_booking = State()

class FeedbackStates(StatesGroup):
    rating = State()
    text = State()

# ================= ДЕКОРАТОРЫ =================
def admin_only(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        for arg in args:
            if isinstance(arg, Message) and arg.from_user.id != ADMIN_ID:
                await arg.answer("⛔ У Вас нет прав администратора")
                return
            if isinstance(arg, CallbackQuery) and arg.from_user.id != ADMIN_ID:
                await arg.answer("⛔ У Вас нет прав администратора", show_alert=True)
                return
        return await func(*args, **kwargs)
    return wrapper

# ================= ОБРАБОТКА КНОПОК ВНИЗУ =================
@dp.message(F.text == "📅 Записаться")
async def btn_book(message: Message, state: FSMContext):
    await state.clear()
    can_book, msg = db.can_make_booking(message.from_user.id)
    if not can_book:
        await message.answer(msg, reply_markup=main_menu_kb())
        return
    await message.answer("📅 Выберите день:", reply_markup=days_inline_kb())

@dp.message(F.text == "💰 Прайс-лист")
async def btn_price(message: Message):
    text = "💰 ПРАЙС-ЛИСТ\n\n"
    for service in PRICES:
        text += f"{service.name}\n"
        text += f"   {service.description}\n"
        text += f"   💵 {service.price}\n"
        text += f"   ⏱ {service.duration} мин.\n\n"
    await message.answer(text, reply_markup=main_menu_kb())

@dp.message(F.text == "📸 Примеры работ")
async def btn_portfolio(message: Message):
    paths = sorted(glob.glob(os.path.join(PORTFOLIO_DIR, "*.jpg")) +
                   glob.glob(os.path.join(PORTFOLIO_DIR, "*.png")))
    if not paths:
        await message.answer("📸 Фото работ пока нет.", reply_markup=main_menu_kb())
        return
    
    try:
        media = [InputMediaPhoto(media=FSInputFile(p)) for p in paths[:10]]
        media[0].caption = "📸 Примеры работ"
        await message.answer_media_group(media)
        await message.answer("📸 Выберите действие:", reply_markup=main_menu_kb())
    except Exception as e:
        logger.error(f"Ошибка портфолио: {e}")
        await message.answer("❌ Ошибка загрузки фото", reply_markup=main_menu_kb())

@dp.message(F.text == "⭐ Отзывы")
async def btn_reviews(message: Message):
    reviews = db.get_approved_feedback()
    if not reviews:
        text = "⭐ Отзывов пока нет. Будьте первыми!"
    else:
        text = "⭐ ОТЗЫВЫ КЛИЕНТОВ\n\n"
        for r in reviews:
            stars = "⭐" * r['rating'] + "☆" * (5 - r['rating'])
            text += f"{r['username'] or 'Аноним'} {stars}\n"
            text += f"📝 {r['text']}\n"
            text += f"📅 {r['created_at'][:10]}\n\n"
    await message.answer(text, reply_markup=main_menu_kb())

@dp.message(F.text == "👩‍🎨 О мастере")
async def btn_about(message: Message):
    about = (
        "👩‍🎨 О МАСТЕРЕ\n\n"
        "Меня зовут Анна, я профессиональный мастер маникюра с 5-летним опытом.\n\n"
        "✨ Что я предлагаю:\n"
        "• Индивидуальный подход\n"
        "• Качественные материалы\n"
        "• Стерильные инструменты\n"
        "• Уютная атмосфера\n\n"
        "📍 Адрес: ул. Примерная, 15\n"
        "⏰ Режим работы: 10:00–20:00"
    )
    await message.answer(about, reply_markup=main_menu_kb())

@dp.message(F.text == "💬 Оставить отзыв")
async def btn_feedback(message: Message, state: FSMContext):
    await state.set_state(FeedbackStates.rating)
    rating_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ 1", callback_data="rating_1"),
            InlineKeyboardButton(text="⭐ 2", callback_data="rating_2"),
            InlineKeyboardButton(text="⭐ 3", callback_data="rating_3"),
            InlineKeyboardButton(text="⭐ 4", callback_data="rating_4"),
            InlineKeyboardButton(text="⭐ 5", callback_data="rating_5")
        ],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu")]
    ])
    await message.answer("⭐ Оцените работу от 1 до 5:", reply_markup=rating_kb)

@dp.message(F.text == "📋 Мои записи")
async def btn_my_bookings(message: Message):
    bookings = db.get_user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("📋 У Вас нет активных записей", reply_markup=main_menu_kb())
        return
    
    text = "📋 ВАШИ ЗАПИСИ\n\n"
    for b in bookings:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"🆔 #{b['id']}\n"
        text += f"📅 {date_label} в {b['time']}\n"
        text += f"👤 {b['first_name']} {b['last_name'] or ''}\n"
        if b['phone']:
            text += f"📞 {b['phone']}\n"
        if b['email']:
            text += f"📧 {b['email']}\n"
        if b['services']:
            text += f"💅 {b['services']}\n"
        text += "\n"
    await message.answer(text, reply_markup=main_menu_kb())

@dp.message(F.text == "❌ Отменить запись")
async def btn_cancel_booking(message: Message, state: FSMContext):
    bookings = db.get_user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("📋 У Вас нет активных записей для отмены", reply_markup=main_menu_kb())
        return
    
    text = "❌ ВЫБЕРИТЕ ЗАПИСЬ ДЛЯ ОТМЕНЫ:\n\n"
    kb_buttons = []
    for b in bookings:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"🆔 #{b['id']} — {date_label} в {b['time']}\n"
        kb_buttons.append([InlineKeyboardButton(
            text=f"❌ Отменить #{b['id']}",
            callback_data=f"cancel_booking_{b['id']}"
        )])
    
    kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

@dp.message(F.text == "⚙️ Админ-панель")
@admin_only
async def btn_admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У Вас нет прав администратора", reply_markup=main_menu_kb())
        return
    await message.answer("⚙️ АДМИН-ПАНЕЛЬ", reply_markup=admin_menu_kb())

@dp.message(F.text == "📋 Все записи")
@admin_only
async def btn_admin_bookings(message: Message):
    bookings = db.get_all_bookings()
    if not bookings:
        await message.answer("📋 Нет активных записей", reply_markup=admin_menu_kb())
        return
    
    text = "📋 ВСЕ ЗАПИСИ\n\n"
    for b in bookings[:20]:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"🆔 #{b['id']} {date_label} {b['time']}\n"
        text += f"   👤 {b['first_name']} {b.get('last_name', '')}\n"
        if b['phone']:
            text += f"   📞 {b['phone']}\n"
        if b['email']:
            text += f"   📧 {b['email']}\n"
        if b['services']:
            text += f"   💅 {b['services']}\n"
        text += f"   🆔 {b['user_id']} @{b.get('tg_username', '—')}\n\n"
    
    await message.answer(text, reply_markup=admin_menu_kb())

@dp.message(F.text == "⭐ Отзывы на модерации")
@admin_only
async def btn_admin_feedback(message: Message):
    feedbacks = db.get_unapproved_feedback()
    if not feedbacks:
        await message.answer("⭐ Нет отзывов на модерации", reply_markup=admin_menu_kb())
        return
    
    fb = feedbacks[0]
    stars = "⭐" * fb['rating'] + "☆" * (5 - fb['rating'])
    await message.answer(
        f"⭐ ОТЗЫВ #{fb['id']}\n\n"
        f"👤 {fb['username'] or fb['user_id']}\n"
        f"Рейтинг: {stars}\n"
        f"📝 {fb['text']}\n"
        f"📅 {fb['created_at']}\n\n"
        f"Осталось {len(feedbacks) - 1} отзывов",
        reply_markup=admin_feedback_inline_kb(fb['id'])
    )

@dp.message(F.text == "📊 Статистика")
@admin_only
async def btn_admin_stats(message: Message):
    bookings = db.get_all_bookings()
    total = len(bookings)
    
    days = {}
    for b in bookings:
        day = b['day']
        days[day] = days.get(day, 0) + 1
    
    text = "📊 СТАТИСТИКА\n\n"
    text += f"Всего записей: {total}\n\n"
    text += "📅 По дням:\n"
    for day, count in sorted(days.items())[:10]:
        d = date.fromisoformat(day)
        text += f"   {d.day:02d}.{d.month:02d}: {count}\n"
    
    await message.answer(text, reply_markup=admin_menu_kb())

@dp.message(F.text == "⬅️ В главное меню")
async def btn_back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())

@dp.message(F.text == "❌ Отменить действие")
async def btn_cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено ✅", reply_markup=main_menu_kb())

# ================= ОСНОВНЫЕ КОМАНДЫ =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    bookings = db.get_user_bookings(message.from_user.id)
    booking_status = ""
    if bookings:
        booking_status = f"\n📋 У Вас есть активные записи:"
        for b in bookings:
            d = date.fromisoformat(b['day'])
            date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
            booking_status += f"\n   📅 {date_label} в {b['time']}"
    
    await message.answer(
        f"👋 Здравствуйте, {message.from_user.first_name}!\n"
        "💅 Добро пожаловать в салон красоты!\n\n"
        "Выберите действие в меню ниже:"
        f"{booking_status}",
        reply_markup=main_menu_kb()
    )

@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(
        f"🆔 Ваш ID: {message.from_user.id}\n"
        f"👤 Ваш username: @{message.from_user.username or 'не указан'}",
        reply_markup=main_menu_kb()
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено ✅", reply_markup=main_menu_kb())

@dp.message(Command("stats"))
@admin_only
async def cmd_stats(message: Message):
    bookings = db.get_all_bookings()
    if not bookings:
        await message.answer("📊 Ближайших записей нет", reply_markup=main_menu_kb())
        return
    
    text = "📊 СТАТИСТИКА ЗАПИСЕЙ\n\n"
    by_day = {}
    for b in bookings:
        day = b['day']
        if day not in by_day:
            by_day[day] = []
        by_day[day].append(b)
    
    for day, items in sorted(by_day.items()):
        d = date.fromisoformat(day)
        day_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"📅 {day_label} — {len(items)} записей\n"
        for item in items:
            text += f"   🕐 {item['time']} — {item['first_name']} {item.get('last_name', '')}"
            if item['phone']:
                text += f" 📞 {item['phone']}"
            text += "\n"
        text += "\n"
    
    await message.answer(text, reply_markup=main_menu_kb())

# ================= INLINE ОБРАБОТЧИКИ =================
@dp.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "book")
async def cb_book(call: CallbackQuery, state: FSMContext):
    await state.clear()
    can_book, msg = db.can_make_booking(call.from_user.id)
    if not can_book:
        await call.message.edit_text(msg)
        await call.answer()
        return
    await call.message.edit_text("📅 Выберите день:", reply_markup=days_inline_kb())
    await call.answer()

@dp.callback_query(F.data == "admin_panel")
@admin_only
async def cb_admin_panel(call: CallbackQuery):
    await call.message.edit_text("⚙️ АДМИН-ПАНЕЛЬ")
    await call.message.answer("⚙️ АДМИН-ПАНЕЛЬ", reply_markup=admin_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "admin_feedback")
@admin_only
async def cb_admin_feedback(call: CallbackQuery):
    feedbacks = db.get_unapproved_feedback()
    if not feedbacks:
        await call.message.edit_text("⭐ Нет отзывов на модерации", reply_markup=back_inline_kb())
        await call.answer()
        return
    
    fb = feedbacks[0]
    stars = "⭐" * fb['rating'] + "☆" * (5 - fb['rating'])
    await call.message.edit_text(
        f"⭐ ОТЗЫВ #{fb['id']}\n\n"
        f"👤 {fb['username'] or fb['user_id']}\n"
        f"Рейтинг: {stars}\n"
        f"📝 {fb['text']}\n"
        f"📅 {fb['created_at']}\n\n"
        f"Осталось {len(feedbacks) - 1} отзывов",
        reply_markup=admin_feedback_inline_kb(fb['id'])
    )
    await call.answer()

# ================= ОСТАЛЬНЫЕ INLINE ОБРАБОТЧИКИ =================
@dp.callback_query(F.data.startswith("day_"))
async def cb_day(call: CallbackQuery, state: FSMContext):
    day = call.data.split("_")[1]
    await state.update_data(day=day)
    kb = times_inline_kb(day)
    d = date.fromisoformat(day)
    label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
    if len(kb.inline_keyboard) <= 1:
        await call.message.edit_text(f"😔 На {label} всё занято", reply_markup=days_inline_kb())
    else:
        await call.message.edit_text(f"🕐 {label} — свободное время:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("time_"))
async def cb_time(call: CallbackQuery, state: FSMContext):
    _, day, t = call.data.split("_", 2)
    await state.update_data(day=day, time=t)
    await state.set_state(BookingStates.first_name)
    await call.message.edit_text(
        f"✅ Вы выбрали: {day} в {t}\n\n"
        "👤 Ваше имя (как к Вам обращаться)?"
    )
    await call.answer()

# ================= ОСТАЛЬНЫЕ ХЕНДЛЕРЫ (FSM) =================
@dp.message(BookingStates.first_name)
async def bk_first_name(message: Message, state: FSMContext):
    await state.update_data(first_name=message.text.strip())
    await state.set_state(BookingStates.last_name)
    await message.answer("👤 Ваша фамилия? (или /skip для пропуска)", reply_markup=cancel_kb())

@dp.message(BookingStates.last_name, Command("skip"))
async def bk_last_name_skip(message: Message, state: FSMContext):
    await state.update_data(last_name="")
    await state.set_state(BookingStates.phone)
    await message.answer("📞 Ваш номер телефона?\nНапример: +7 999 123-45-67", reply_markup=cancel_kb())

@dp.message(BookingStates.last_name)
async def bk_last_name(message: Message, state: FSMContext):
    await state.update_data(last_name=message.text.strip())
    await state.set_state(BookingStates.phone)
    await message.answer("📞 Ваш номер телефона?\nНапример: +7 999 123-45-67", reply_markup=cancel_kb())

@dp.message(BookingStates.phone)
async def bk_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await state.set_state(BookingStates.email)
    await message.answer("📧 Ваш email?\n(или /skip для пропуска)", reply_markup=cancel_kb())

@dp.message(BookingStates.email, Command("skip"))
async def bk_email_skip(message: Message, state: FSMContext):
    await state.update_data(email="")
    await state.set_state(BookingStates.services)
    await show_services(message, state)

@dp.message(BookingStates.email)
async def bk_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if "@" not in email or "." not in email:
        await message.answer("⚠️ Похоже, это не email. Введите корректный email или /skip")
        return
    await state.update_data(email=email)
    await state.set_state(BookingStates.services)
    await show_services(message, state)

async def show_services(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_services", [])
    text = "💅 ВЫБЕРИТЕ УСЛУГИ\n\n"
    if selected:
        text += "✅ Выбрано:\n"
        for s in selected:
            text += f"   • {s}\n"
        text += "\n"
    text += "Нажмите на услугу, чтобы выбрать/отменить.\n"
    text += "Можно выбрать несколько услуг."
    
    await message.answer(text, reply_markup=services_inline_kb(selected))

@dp.callback_query(F.data.startswith("svc_"))
async def cb_service_toggle(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_services", [])
    idx = int(call.data.split("_")[1])
    service_name = PRICES[idx].name
    
    if service_name in selected:
        selected.remove(service_name)
    else:
        selected.append(service_name)
    
    await state.update_data(selected_services=selected)
    await call.message.edit_text(
        "💅 ВЫБЕРИТЕ УСЛУГИ\n\n"
        f"✅ Выбрано: {', '.join(selected) if selected else 'ничего не выбрано'}\n\n"
        "Нажмите на услугу, чтобы выбрать/отменить.\n"
        "Можно выбрать несколько услуг.",
        reply_markup=services_inline_kb(selected)
    )
    await call.answer()

@dp.callback_query(F.data == "services_clear")
async def cb_services_clear(call: CallbackQuery, state: FSMContext):
    await state.update_data(selected_services=[])
    await call.message.edit_text(
        "💅 ВЫБЕРИТЕ УСЛУГИ\n\n"
        "✅ Выбрано: ничего не выбрано\n\n"
        "Нажмите на услугу, чтобы выбрать.\n"
        "Можно выбрать несколько услуг.",
        reply_markup=services_inline_kb([])
    )
    await call.answer()

@dp.callback_query(F.data == "services_done")
async def cb_services_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_services", [])
    
    if not selected:
        await call.answer("⚠️ Выберите хотя бы одну услугу!", show_alert=True)
        return
    
    await state.update_data(services=selected)
    await state.set_state(BookingStates.comment)
    await call.message.edit_text(
        "📝 Комментарий к записи?\n"
        "(например, особые пожелания, или /skip для пропуска)"
    )
    await call.answer()

@dp.message(BookingStates.comment, Command("skip"))
async def bk_comment_skip(message: Message, state: FSMContext):
    await state.update_data(comment="")
    await show_confirm(message, state)

@dp.message(BookingStates.comment)
async def bk_comment(message: Message, state: FSMContext):
    await state.update_data(comment=message.text.strip())
    await show_confirm(message, state)

async def show_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    d = date.fromisoformat(data["day"])
    date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]}) {data['time']}"
    
    services_text = "\n".join([f"   • {s}" for s in data.get("services", [])])
    
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="change_data")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="menu")]
    ])
    
    await message.answer(
        f"📋 ПРОВЕРЬТЕ ЗАПИСЬ:\n\n"
        f"📅 {date_label}\n"
        f"👤 {data['first_name']} {data.get('last_name', '')}\n"
        f"📞 {data.get('phone', '—')}\n"
        f"📧 {data.get('email', '—')}\n"
        f"💅 Услуги:\n{services_text}\n"
        f"📝 {data.get('comment', 'Без комментария')}\n\n"
        "Всё верно?",
        reply_markup=confirm_kb
    )

@dp.callback_query(F.data == "change_data")
async def cb_change_data(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Изменить имя", callback_data="change_first_name")],
        [InlineKeyboardButton(text="👤 Изменить фамилию", callback_data="change_last_name")],
        [InlineKeyboardButton(text="📞 Изменить телефон", callback_data="change_phone")],
        [InlineKeyboardButton(text="📧 Изменить email", callback_data="change_email")],
        [InlineKeyboardButton(text="💅 Изменить услуги", callback_data="change_services")],
        [InlineKeyboardButton(text="📝 Изменить комментарий", callback_data="change_comment")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_confirm")]
    ])
    await call.message.edit_text("✏️ Что хотите изменить?", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "back_to_confirm")
async def cb_back_to_confirm(call: CallbackQuery, state: FSMContext):
    await show_confirm(call.message, state)
    await call.answer()

@dp.callback_query(F.data == "change_first_name")
async def cb_change_first_name(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.first_name)
    await call.message.edit_text("👤 Введите новое имя:")
    await call.answer()

@dp.callback_query(F.data == "change_last_name")
async def cb_change_last_name(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.last_name)
    await call.message.edit_text("👤 Введите новую фамилию (или /skip):")
    await call.answer()

@dp.callback_query(F.data == "change_phone")
async def cb_change_phone(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.phone)
    await call.message.edit_text("📞 Введите новый телефон:")
    await call.answer()

@dp.callback_query(F.data == "change_email")
async def cb_change_email(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.email)
    await call.message.edit_text("📧 Введите новый email (или /skip):")
    await call.answer()

@dp.callback_query(F.data == "change_services")
async def cb_change_services(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.services)
    data = await state.get_data()
    selected = data.get("selected_services", data.get("services", []))
    await state.update_data(selected_services=selected)
    await call.message.edit_text(
        "💅 Выберите услуги:",
        reply_markup=services_inline_kb(selected)
    )
    await call.answer()

@dp.callback_query(F.data == "change_comment")
async def cb_change_comment(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.comment)
    await call.message.edit_text("📝 Введите новый комментарий (или /skip):")
    await call.answer()

# ================= ПОДТВЕРЖДЕНИЕ =================
@dp.callback_query(F.data == "confirm_yes")
async def cb_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    if not data or not data.get("day") or not data.get("time"):
        await call.message.edit_text("❌ Ошибка: данные не найдены. Попробуйте заново.", reply_markup=main_menu_kb())
        await call.answer()
        return
    
    success, message = db.save_booking(
        call.from_user.id,
        call.from_user.username or "",
        data["day"],
        data["time"],
        data.get("first_name", "Клиент"),
        data.get("last_name", ""),
        data.get("phone", ""),
        data.get("email", ""),
        data.get("services", []),
        data.get("comment", "")
    )
    
    if success:
        d = date.fromisoformat(data["day"])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]}) {data['time']}"
        services_text = "\n".join([f"   • {s}" for s in data.get("services", [])])
        
        await call.message.edit_text(
            f"✅ ЗАПИСЬ ПОДТВЕРЖДЕНА!\n\n"
            f"📅 {date_label}\n"
            f"👤 {data['first_name']} {data.get('last_name', '')}\n"
            f"📞 {data.get('phone', '—')}\n"
            f"📧 {data.get('email', '—')}\n"
            f"💅 Услуги:\n{services_text}\n"
            f"📝 {data.get('comment', 'Без комментария')}\n\n"
            "✨ Ждём Вас!",
            reply_markup=main_menu_kb()
        )
        
        if NOTIFICATION_CHAT_ID:
            try:
                notification = (
                    f"🆕 НОВАЯ ЗАПИСЬ!\n\n"
                    f"📅 {date_label}\n"
                    f"👤 {data['first_name']} {data.get('last_name', '')}\n"
                    f"📞 {data.get('phone', '—')}\n"
                    f"📧 {data.get('email', '—')}\n"
                    f"💅 Услуги:\n{services_text}\n"
                    f"📝 {data.get('comment', 'Без комментария')}\n"
                    f"🆔 ID: {call.from_user.id}\n"
                    f"👤 TG: @{call.from_user.username or 'не указан'}"
                )
                await bot.send_message(NOTIFICATION_CHAT_ID, notification)
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление: {e}")
        
    else:
        await call.message.edit_text(message, reply_markup=main_menu_kb())
    
    await state.clear()
    await call.answer()

# ================= ОТЗЫВЫ (FSM) =================
@dp.callback_query(F.data.startswith("rating_"))
async def cb_rating(call: CallbackQuery, state: FSMContext):
    rating = int(call.data.split("_")[1])
    await state.update_data(rating=rating)
    await state.set_state(FeedbackStates.text)
    await call.message.edit_text(f"⭐ Ваша оценка: {rating}\n\n📝 Напишите отзыв:")
    await call.answer()

@dp.message(FeedbackStates.text)
async def fb_text(message: Message, state: FSMContext):
    data = await state.get_data()
    db.save_feedback(message.from_user.id, message.from_user.username or "", data['rating'], message.text)
    await state.clear()
    await message.answer("✅ Спасибо за отзыв! ❤️\nПосле модерации он появится в разделе отзывов.", reply_markup=main_menu_kb())
    
    if ADMIN_ID:
        try:
            stars = "⭐" * data['rating']
            await bot.send_message(ADMIN_ID, f"📩 Новый отзыв!\nОценка: {stars}\nТекст: {message.text}")
        except:
            pass

# ================= ОТМЕНА ЗАПИСИ (INLINE) =================
@dp.callback_query(F.data.startswith("cancel_booking_"))
async def cb_cancel_booking(call: CallbackQuery):
    booking_id = int(call.data.split("_")[2])
    success, message = db.cancel_booking(booking_id, call.from_user.id)
    await call.message.edit_text(message, reply_markup=main_menu_kb())
    await call.answer()

# ================= АДМИН (INLINE) =================
@dp.callback_query(F.data.startswith("admin_approve_fb_"))
@admin_only
async def cb_admin_approve_feedback(call: CallbackQuery):
    fb_id = int(call.data.split("_")[3])
    db.approve_feedback(fb_id)
    await call.message.edit_text("✅ Отзыв одобрен", reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("admin_reject_fb_"))
@admin_only
async def cb_admin_reject_feedback(call: CallbackQuery):
    fb_id = int(call.data.split("_")[3])
    with db.get_connection() as conn:
        conn.execute("DELETE FROM feedback WHERE id = ?", (fb_id,))
    await call.message.edit_text("❌ Отзыв отклонен", reply_markup=main_menu_kb())
    await call.answer()

# ================= ЗАПУСК =================
async def main():
    try:
        db.init_db()
        logger.info("✅ База данных инициализирована")
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Вебхук удален")
        logger.info("🚀 Бот запущен!")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
