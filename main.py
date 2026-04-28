import aiohttp
import asyncio
import logging
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InaccessibleMessage
from aiogram.filters.callback_data import CallbackData
from sqlalchemy import Column, Integer, String, DateTime, JSON, BigInteger, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from deep_translator import GoogleTranslator, LingueeTranslator
from aiogram.client.session.aiohttp import AiohttpSession

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YANDEX_DICT_KEY = os.getenv("YANDEX_DICT_KEY")
assert BOT_TOKEN is not None and YANDEX_DICT_KEY is not None, "BOT_TOKEN and YANDEX_DICT_KEY must be set"

PROXY_URL = "http://xray1:xray1@vpn-proxy:1080"

engine = create_async_engine("sqlite+aiosqlite:///words.db")
async_session = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()

INTERVALS = [
    timedelta(minutes=20), timedelta(hours=2), timedelta(hours=22), timedelta(days=4),
    timedelta(days=11), timedelta(weeks=5), timedelta(weeks=15), timedelta(weeks=36),
    timedelta(weeks=80), timedelta(weeks=200)
]
INTERVALS_STR = ["20m", "2h", "22h", "4d", "11d", "5w", "15w", "36w", "80w", "200w"]


class Word(Base):
    __tablename__ = 'words'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    word_en: Mapped[str]
    translations_ru: Mapped[list] = mapped_column(JSON)
    saved_at: Mapped[datetime] = mapped_column(default=datetime.now)
    next_repeat_time: Mapped[datetime]
    interval_index: Mapped[int] = mapped_column(default=0)


class DelWord(CallbackData, prefix="del"):
    id: int


class ForgotWord(CallbackData, prefix="fgt"):
    id: int


session = AiohttpSession(proxy=PROXY_URL)


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode='HTML'), session=session)
logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

from aiogram.filters import CommandStart

@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "👋 <b>Привет! Я бот-переводчик с функцией интервального повторения слов. Суть в том, что можно заменить обычный переводчик, если нужно узнать перевод незнакомого слова</b>\n\n"
        "Моя цель — помочь тебе не просто перевести незнакомое английское слово, но и выучить его навсегда с помощью <i>интервального повторения</i>.\n\n"
        "<b>Как я работаю:</b>\n"
        "🇬🇧 Отправь мне любое слово на английском.\n"
        "🇷🇺 Я переведу его и автоматически добавлю в твой личный словарь.\n"
        "Я буду присылать тебе слово для повторения через научно обоснованные интервалы времени (через 20 минут, пару часов, день, неделю и т.д.).\n\n"
        "Если ты забудешь перевод — не страшно! Нажми кнопку «Не вспомнил», и я уменьшу интервал, чтобы ты точно его закрепил.\n\n"
        "👇 <i>Отправь мне свое первое английское слово прямо сейчас!</i>"
    )
    await message.answer(text)

async def translate_word(word: str) -> list[str]:
    try:
        translated_text = await asyncio.to_thread(
            GoogleTranslator(
                source='en',
                target='ru',
                proxies={'http': PROXY_URL, 'https': PROXY_URL}
            ).translate,
            word
        )
        translationGoogle = list(dict.fromkeys([t.strip().lower() for t in translated_text.split(',')]))

        url = "https://dictionary.yandex.net/api/v1/dicservice.json/lookup"
        params = {
            "key": YANDEX_DICT_KEY or "",
            "lang": "en-ru",
            "text": word
        }

        async with aiohttp.ClientSession() as http_session:
            async with http_session.get(url, params=params, proxy=PROXY_URL) as response:
                data = await response.json()

                translationsYandex =[]
                if 'def' in data and data['def']:
                    for pos_block in data['def']:
                        for tr in pos_block.get('tr',[]):
                            translationsYandex.append(tr['text'].lower())
                            if 'syn' in tr:
                                for syn in tr['syn']:
                                    translationsYandex.append(syn['text'].lower())

        translations = list(dict.fromkeys(translationGoogle + translationsYandex))

        return translations[:5]

    except Exception as e:
        print(f"Translation error: {e}")
        return ["перевод_не_найден"]


@dp.message(F.text)
async def add_word(message: Message):
    assert message.text is not None
    en_word = message.text.strip().lower()
    translations = await translate_word(en_word)

    msg = await message.answer(
        f"🇬🇧 <b>{en_word}</b>\n🇷🇺 {', '.join(translations)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Удалить из базы", callback_data=DelWord(id=0).pack())
        ]]) if translations != ["перевод_не_найден"] else None
    )

    if translations == ["перевод_не_найден"]:
        return

    async with async_session() as session:
        assert message.from_user is not None
        new_word = Word(
            user_id=message.from_user.id,
            message_id=msg.message_id,
            word_en=en_word,
            translations_ru=translations,
            next_repeat_time=datetime.now() + INTERVALS[0],
            interval_index=0
        )
        session.add(new_word)
        await session.flush()

        await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Удалить из базы", callback_data=DelWord(id=new_word.id).pack())
        ]]))
        await session.commit()

    await asyncio.sleep(30)
    async with async_session() as session:
        word = await session.get(Word, new_word.id)
        if word:
            await msg.edit_text(f"🇬🇧 <b>{en_word}</b>\n🇷🇺 <tg-spoiler>{', '.join(translations)}</tg-spoiler>")
            await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Удалить из базы", callback_data=DelWord(id=new_word.id).pack())
            ]]))
        else:
            pass


@dp.callback_query(DelWord.filter())
async def del_word(query: CallbackQuery, callback_data: DelWord):
    async with async_session() as session:
        word = await session.get(Word, callback_data.id)
        if word:
            await session.delete(word)
            await session.commit()
            assert isinstance(query.message, Message)
            await query.message.edit_text(f"<s>{query.message.text}</s>\nУдалено из базы.")
        else:
            await query.answer("Слово не найдено в базе.")


@dp.callback_query(ForgotWord.filter())
async def forgot_word(query: CallbackQuery, callback_data: ForgotWord):
    async with async_session() as session:
        word = await session.get(Word, callback_data.id)
        if word:
            word.interval_index = max(0, word.interval_index - 1)
            word.next_repeat_time = datetime.now() + INTERVALS[word.interval_index]
            await session.commit()
            await query.answer(f"Интервал уменьшен. Повтор через {INTERVALS_STR[word.interval_index]}", show_alert=True)
            assert isinstance(query.message, Message)
            await query.message.edit_reply_markup(reply_markup=None)
        else:
            await query.answer("Слово не найдено.")


async def scheduler():
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        async with async_session() as session:
            result = await session.execute(select(Word).where(Word.next_repeat_time <= now))
            words = result.scalars().all()
            for word in words:
                next_idx = min(word.interval_index + 1, len(INTERVALS) - 1)
                word.interval_index = next_idx
                word.next_repeat_time = now + INTERVALS[next_idx]

                text = (
                    f"<b>{word.word_en}</b>\n"
                    f"<tg-spoiler>{', '.join(word.translations_ru)}</tg-spoiler>\n\n"
                    f"Повторение слова через {INTERVALS_STR[max(next_idx - 1, 0)]}. Следующее -- через {INTERVALS_STR[next_idx]}"
                )
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Не вспомнил", callback_data=ForgotWord(id=word.id).pack())
                ]])
                try:
                    await bot.send_message(chat_id=word.user_id, text=text, reply_markup=markup, reply_to_message_id=word.message_id)
                except Exception:
                    pass
            if words:
                await session.commit()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
