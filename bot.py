import os
import re
import random
import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from telethon import TelegramClient, errors, functions
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, BlockRequest

BOT_TOKEN = "8892329170:AAFQpzPhO_nsjyPSeRRNXzN_qgyNNj-bY9k"
API_ID = 39813941
API_HASH = "6aa1f649d2a6b3a308bc27aed9e6de29"
SESSIONS_DIR = "./sessions"

os.makedirs(SESSIONS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CLIENTS = {}
CODE_HASHES = {}
PHONES = {}

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
                    logging.info(f"left: {d.name}")
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
            logging.info(f"contacts removed: {len(ids)}")
        except Exception as e:
            logging.warning(f"contacts fail: {e}")
    if block:
        for uid in ids:
            try:
                await safe(lambda u=uid: cli(BlockRequest(id=u)), f"block {uid}")
                await human_delay(1, 3)
            except Exception as e:
                logging.warning(f"block {uid}: {e}")


async def full_wipe(cli, leave_channels=True, drop_contacts=True):
    await clean_private(cli)
    await clean_groups(cli)
    await clean_channels(cli, leave=leave_channels)
    await clean_bots(cli)
    if drop_contacts:
        await clean_contacts(cli, remove=True, block=False)


class Auth(StatesGroup):
    phone = State()
    code = State()
    twofa = State()


bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


async def cleanup_client(chat_id: int):
    cli = CLIENTS.pop(chat_id, None)
    if cli:
        try:
            await cli.disconnect()
        except Exception:
            pass
    CODE_HASHES.pop(chat_id, None)
    PHONES.pop(chat_id, None)


def get_authed(chat_id: int) -> Optional[TelegramClient]:
    return CLIENTS.get(chat_id)


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await cleanup_client(msg.chat.id)
    await state.set_state(Auth.phone)
    await msg.answer(
        "Кинь номер: +7999..., +375..., +1..., +222...\n"
        "/cancel — сброс."
    )


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await cleanup_client(msg.chat.id)
    await msg.answer("Сброшено.")


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "/start — войти (номер → код → 2FA)\n"
        "/cancel — сброс\n"
        "/status — статус сессии\n"
        "/logout — отключить\n"
        "/clean_private — личка\n"
        "/clean_groups — группы\n"
        "/clean_channels — каналы + выйти\n"
        "/clean_bots — боты\n"
        "/clean_contacts — контакты\n"
        "/wipe — всё сразу"
    )


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    cli = CLIENTS.get(msg.chat.id)
    if not cli:
        return await msg.answer("Сессии нет. /start")
    if await cli.is_user_authorized():
        me = await cli.get_me()
        return await msg.answer(f"Залогинен: {me.first_name or ''} (@{me.username or me.id})")
    await msg.answer(f"Не завершён вход: {PHONES.get(msg.chat.id)}")


@dp.message(Command("logout"))
async def cmd_logout(msg: Message, state: FSMContext):
    await state.clear()
    await cleanup_client(msg.chat.id)
    await msg.answer("Отключено.")


@dp.message(Auth.phone)
async def step_phone(msg: Message, state: FSMContext):
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
        await msg.answer("Номер невалиден.")
        await state.clear()
        await cleanup_client(msg.chat.id)
    except errors.FloodWaitError as e:
        await msg.answer(f"FloodWait {e.seconds}s.")
        await state.clear()
        await cleanup_client(msg.chat.id)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")
        await state.clear()
        await cleanup_client(msg.chat.id)


@dp.message(Auth.code)
async def step_code(msg: Message, state: FSMContext):
    code = re.sub(r"\D", "", msg.text or "")
    if not (4 <= len(code) <= 7):
        return await msg.answer("Код — цифры, 4-7 знаков.")

    cli = CLIENTS.get(msg.chat.id)
    phone = PHONES.get(msg.chat.id)
    if not cli or not phone:
        await state.clear()
        return await msg.answer("Сессия потеряна. /start заново.")

    try:
        await cli.sign_in(phone=phone, code=code, phone_code_hash=CODE_HASHES[msg.chat.id])
        me = await cli.get_me()
        await state.clear()
        await msg.answer(
            f"Зашли как {me.first_name or ''} (@{me.username or me.id}).\n"
            "/help — команды чистки."
        )
    except errors.SessionPasswordNeededError:
        await state.set_state(Auth.twofa)
        await msg.answer("Включена 2FA. Кидай пароль.")
    except errors.PhoneCodeInvalidError:
        await msg.answer("Код неверный.")
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
    cli = CLIENTS.get(msg.chat.id)
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
            f"2FA принята. Зашли как {me.first_name or ''} (@{me.username or me.id}).\n"
            "/help — команды чистки."
        )
    except errors.PasswordHashInvalidError:
        await msg.answer("Пароль неверный.")
    except errors.FloodWaitError as e:
        await msg.answer(f"FloodWait {e.seconds}s.")
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")


async def run_clean(msg: Message, coro, label: str):
    cli = get_authed(msg.chat.id)
    if not cli or not await cli.is_user_authorized():
        return await msg.answer("Сначала /start и войди.")
    await msg.answer(f"{label}: начал. Это долго, паузы между действиями.")
    try:
        await coro(cli)
        await msg.answer(f"{label}: готово.")
    except Exception as e:
        await msg.answer(f"{label}: ошибка {e}")


@dp.message(Command("clean_private"))
async def cmd_clean_private(msg: Message):
    await run_clean(msg, clean_private, "личка")


@dp.message(Command("clean_groups"))
async def cmd_clean_groups(msg: Message):
    await run_clean(msg, clean_groups, "группы")


@dp.message(Command("clean_channels"))
async def cmd_clean_channels(msg: Message):
    await run_clean(msg, lambda c: clean_channels(c, leave=True), "каналы")


@dp.message(Command("clean_bots"))
async def cmd_clean_bots(msg: Message):
    await run_clean(msg, clean_bots, "боты")


@dp.message(Command("clean_contacts"))
async def cmd_clean_contacts(msg: Message):
    await run_clean(msg, lambda c: clean_contacts(c, remove=True, block=False), "контакты")


@dp.message(Command("wipe"))
async def cmd_wipe(msg: Message):
    await run_clean(msg, full_wipe, "ПОЛНАЯ чистка")


@dp.message()
async def fallback(msg: Message):
    await msg.answer("Начни с /start. /help — команды.")


async def main():
    logging.info("auth-bot up")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
