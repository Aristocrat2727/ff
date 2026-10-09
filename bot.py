import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

TOKEN = "8978721220:AAErjYwpzkggnoCIC-NNNS_7Jm8xI1mA3as"  # ← сюда вставь токен строкой

dp = Dispatcher()

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(f"Привет, {message.from_user.full_name}! Я эхо-бот.")

@dp.message(F.text)
async def echo(message: Message):
    await message.answer(f"Ты написал: {message.text}")

async def main():
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
