# language: Python, file: authbot.py
# Telegram auth-bot: phone -> code -> 2FA, multi-country, via env

import os
import re
import asyncio
import logging

from dotenv import load_dotenv
from telethon import TelegramClient, events, errors, functions

load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN")
API_ID      = int(os.getenv("API_ID", "0"))
API_HASH    = os.getenv("API_HASH")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "./sessions")
ADMIN_IDS   = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

if not all([BOT_TOKEN, API_ID, API_HASH]):
    raise SystemExit("BOT_TOKEN / API_ID / API_HASH не заданы в .env")

os.makedirs(SESSIONS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---- хранилище состояний по chat_id ----
# {
#   chat_id: {
#     "client": TelegramClient|None,
#     "phone": "+7999...",
#     "code": "12345"|None,
#     "password": "..."|None,
#     "step": "phone"|"code"|"2fa"|"done"
#   }
# }
SESSIONS: dict[int, dict] = {}


# ---------- нормализация номера ----------

# Допускаем: +7..., 8..., 375..., +1..., +222..., пробелы/скобки/дефисы.
PHONE_RE = re.compile(r"^\+?\d{7,15}$")

def normalize_phone(raw: str) -> str | None:
    """
    Приводим к E.164 (+<цифры>).
    - убираем пробелы, дефисы, скобки
    - 8XXXXXXXXXX (RU/РФ) -> +7XXXXXXXXXX
    - если нет ведущего '+' — просто ставим его
    """
    s = re.sub(r"[\s\-\(\)]", "", raw.strip())
    if not s.startswith("+"):
        if s.startswith("8") and len(s) == 11:
            s = "+7" + s[1:]          # 8xxx -> +7xxx (RU)
        else:
            s = "+" + s
    if not PHONE_RE.match(s):
        return None
    return s


# ---------- создание клиента под конкретного юзера ----------

def new_client(phone: str) -> TelegramClient:
    """Отдельная сессия на номер."""
    safe = phone.lstrip("+")
    path = os.path.join(SESSIONS_DIR, safe)
    return TelegramClient(path, API_ID, API_HASH)


# ---------- бот ----------

bot = TelegramClient(
    os.path.join(SESSIONS_DIR, "bot"), API_ID, API_HASH
).start(bot_token=BOT_TOKEN)


def allowed(event) -> bool:
    if not ADMIN_IDS:
        return True
    return event.chat_id in ADMIN_IDS


@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    if not allowed(event):
        return await event.reply("Не для тебя.")
    SESSIONS.pop(event.chat_id, None)
    SESSIONS[event.chat_id] = {
        "client": None, "phone": None,
        "code": None, "password": None, "step": "phone",
    }
    await event.reply(
        "Кинь номер в формате E.164 (+7999..., +375..., +1..., +222...).\n"
        "Скинуть в любой момент /cancel."
    )


@bot.on(events.NewMessage(pattern=r"^/cancel"))
async def cmd_cancel(event):
    st = SESSIONS.pop(event.chat_id, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass
    await event.reply("Сброшено.")


@bot.on(events.NewMessage())
async def router(event):
    if not allowed(event):
        return
    if event.text.startswith("/"):
        return  # команды выше

    st = SESSIONS.get(event.chat_id)
    if not st:
        return await event.reply("Сначала /start.")

    step = st["step"]
    text = event.text.strip()

    # --- шаг 1: телефон ---
    if step == "phone":
        phone = normalize_phone(text)
        if not phone:
            return await event.reply("Не похоже на номер. Пример: +79991234567")
        st["phone"] = phone

        client = new_client(phone)
        st["client"] = client

        await event.reply(f"Отправляю код на {phone}...")
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            st["phone_code_hash"] = sent.phone_code_hash
            st["step"] = "code"
            await event.reply("Код ушёл в Telegram/SMS. Кидай сюда.")
        except errors.PhoneNumberInvalidError:
            await event.reply("Номер невалиден для Telegram.")
            SESSIONS.pop(event.chat_id, None)
        except errors.FloodWaitError as e:
            await event.reply(f"FloodWait {e.seconds}s. Пробуй позже.")
            SESSIONS.pop(event.chat_id, None)
        except Exception as e:
            await event.reply(f"Ошибка: {e}")
            SESSIONS.pop(event.chat_id, None)

    # --- шаг 2: код ---
    elif step == "code":
        code = re.sub(r"\D", "", text)
        if not (4 <= len(code) <= 7):
            return await event.reply("Код — только цифры, 4–7 знаков.")
        st["code"] = code
        client = st["client"]

        try:
            await client.sign_in(
                phone=st["phone"],
                code=code,
                phone_code_hash=st["phone_code_hash"],
            )
            st["step"] = "done"
            me = await client.get_me()
            await event.reply(
                f"Зашли как {me.first_name or ''} (@{me.username or me.id}).\n"
                f"Сессия: {SESSIONS_DIR}/{st['phone'].lstrip('+')}.session"
            )
        except errors.SessionPasswordNeededError:
            st["step"] = "2fa"
            await event.reply("Включена 2FA. Кидай пароль.")
        except errors.PhoneCodeInvalidError:
            await event.reply("Код неверный. Попробуй ещё раз.")
        except errors.PhoneCodeExpiredError:
            await event.reply("Код протух. /start заново.")
            SESSIONS.pop(event.chat_id, None)
        except errors.FloodWaitError as e:
            await event.reply(f"FloodWait {e.seconds}s.")
        except Exception as e:
            await event.reply(f"Ошибка: {e}")

    # --- шаг 3: 2FA ---
    elif step == "2fa":
        st["password"] = text
        client = st["client"]
        try:
            await client.sign_in(password=text)
            st["step"] = "done"
            me = await client.get_me()
            await event.reply(
                f"2FA принята. Зашли как {me.first_name or ''} "
                f"(@{me.username or me.id})."
            )
        except errors.PasswordHashInvalidError:
            await event.reply("Пароль неверный. Ещё раз.")
        except errors.FloodWaitError as e:
            await event.reply(f"FloodWait {e.seconds}s.")
        except Exception as e:
            await event.reply(f"Ошибка: {e}")

    elif step == "done":
        await event.reply("Уже залогинены. /start чтобы заново.")


async def main():
    logging.info("auth-bot up")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
