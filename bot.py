# language: Python, file: authbot_aiogram.py
# aiogram 3.x front + Telethon backend, Railway env vars, no .env file

import os
import re
import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from telethon import TelegramClient, errors

# ---------- env (Railway → Variables) ----------

def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"env {name} не задан. Добавь в Railway → Variables.")
    return v

BOT_TOKEN    = _req("BOT_TOKEN")
API_ID       = int(_req("API_ID"))
API_HASH     = _req("API_HASH")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "./sessions")
ADMIN_IDS    = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
}

os.makedirs(SESSIONS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ---------- state ----------

CLIENTS:     dict[int, TelegramClient] = {}
CODE_HASHES: dict[int, str]            = {}
PHONES:      dict[int, str]            = {}


# ---------- phone ----------

PHONE_RE = re.compile(r"^\+?\d{7,15}$")

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


def new_client(phone: str) -> TelegramClient:
    safe = phone.lstrip("+")
    path = os.path.join(SESSIONS_DIR, safe)
    return TelegramClient(path, API_ID, API_HASH)


# ---------- FSM ----------

class Auth(StatesGroup):
    phone = State()
    code  = State()
    twofa = State()


bot = Bot(BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


def allowed(msg: Message) -> bool:
    if not ADMIN_IDS:
        return True
    return msg.chat.id in ADMIN_IDS


async def cleanup_client(chat_id: int):
    cli = CLIENTS.pop(chat_id, None)
    if cli:
        try:
            await cli.disconnect()
        except Exception:
            pass
    CODE_HASHES.pop(chat_id, None)
    PHONES.pop(chat_id, None)


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    if not allowed(msg):
        return await msg.answer("Не для тебя.")
    await state.clear()
    await cleanup_client(msg.chat.id)
    await state.set_state(Auth.phone)
    await msg.answer(
        "Кинь номер (E.164): +7999..., +375..., +1..., +222...\n"
        "/cancel — сбросить."
    )


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await cleanup_client(msg.chat.id)
    await msg.answer("Сброшено.")


@dp.message(Auth.phone)
async def step_phone(msg: Message, state: FSMContext):
    if not allowed(msg):
        return
    phone = normalize_phone(msg.text or "")
    if not phone:
        return await msg.answer("Не похоже на номер. Пример: +79991234567")

    cli = new_client(phone)
    CLIENTS[msg.chat.id] = cli
    PHONES[msg.chat.id] = phone

    await msg.answer(f"Отправляю код на {phone}...")
    try:
        await cli.connect()
        sent = await cli.send_code_request(phone)
        CODE_HASHES[msg.chat.id] = sent.phone_code_hash
        await state.set_state(Auth.code)
        await msg.answer("Код ушёл в Telegram/SMS. Кидай сюда.")
    except errors.PhoneNumberInvalidError:
        await msg.answer("Номер невалиден для Telegram.")
        await state.clear()
        await cleanup_client(msg.chat.id)
    except errors.FloodWaitError as e:
        await msg.answer(f"FloodWait {e.seconds}s. Пробуй позже.")
        await state.clear()
        await cleanup_client(msg.chat.id)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")
        await state.clear()
        await cleanup_client(msg.chat.id)


@dp.message(Auth.code)
async def step_code(msg: Message, state: FSMContext):
    if not allowed(msg):
        return
    code = re.sub(r"\D", "", msg.text or "")
    if not (4 <= len(code) <= 7):
        return await msg.answer("Код — только цифры, 4–7 знаков.")

    cli   = CLIENTS.get(msg.chat.id)
    phone = PHONES.get(msg.chat.id)
    if not cli or not phone:
        await state.clear()
        return await msg.answer("Сессия потеряна. /start заново.")

    try:
        await cli.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=CODE_HASHES[msg.chat.id],
        )
        me = await cli.get_me()
        await state.clear()
        await msg.answer(
            f"Зашли как {me.first_name or ''} (@{me.username or me.id}).\n"
            f"Сессия: {SESSIONS_DIR}/{phone.lstrip('+')}.session"
        )
    except errors.SessionPasswordNeededError:
        await state.set_state(Auth.twofa)
        await msg.answer("Включена 2FA. Кидай пароль.")
    except errors.PhoneCodeInvalidError:
        await msg.answer("Код неверный. Попробуй ещё раз.")
    except errors.PhoneCodeExpiredError:
        await msg.answer("Код протух. /start заново.")
        await state.clear()
        await cleanup_client(msg.chat.id)
    except errors.FloodWaitError as e:
        await msg.answer(f"FloodWait {e.seconds}s.")
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")


@dp.message(Auth.twofa)
async def step_2fa(msg: Message, state: FSMContext):
    if not allowed(msg):
        return
    cli   = CLIENTS.get(msg.chat.id)
    phone = PHONES.get(msg.chat.id)
    if not cli or not phone:
        await state.clear()
        return await msg.answer("Сессия потеряна. /start заново.")

    try:
        await cli.sign_in(password=msg.text or "")
        me = await cli.get_me()
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(
            f"2FA принята. Зашли как {me.first_name or ''} "
            f"(@{me.username or me.id}).\n"
            f"Сессия: {SESSIONS_DIR}/{phone.lstrip('+')}.session"
        )
    except errors.PasswordHashInvalidError:
        await msg.answer("Пароль неверный. Ещё раз.")
    except errors.FloodWaitError as e:
        await msg.answer(f"FloodWait {e.seconds}s.")
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")


@dp.message()
async def fallback(msg: Message):
    if not allowed(msg):
        return
    await msg.answer("Начни с /start.")


async def main():
    logging.info("auth-bot up on Railway")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
