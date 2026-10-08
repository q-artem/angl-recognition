import aiohttp
import asyncio
import html
import logging
import os
import re
import ssl
import time
import uuid
from dotenv import load_dotenv
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InaccessibleMessage, ReplyParameters, BufferedInputFile
from aiogram.filters.callback_data import CallbackData
from sqlalchemy import Column, Integer, String, DateTime, JSON, BigInteger, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from aiogram.client.session.aiohttp import AiohttpSession

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YANDEX_DICT_KEY = os.getenv("YANDEX_DICT_KEY")
assert BOT_TOKEN is not None and YANDEX_DICT_KEY is not None, "BOT_TOKEN and YANDEX_DICT_KEY must be set"
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)  # кому доступна команда bd; 0 — никому
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")  # без ключа переводит только словарь Яндекса

PROXY_URL = "http://xray:xray@vpn-proxy:1080"

engine = create_async_engine("sqlite+aiosqlite:///data/words.db")
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


BD_MAX_ROWS = 1000


# должен стоять раньше add_word, иначе запрос уйдёт в перевод
@dp.message(F.from_user.id == ADMIN_ID, F.text.regexp(r"(?is)^bd\s+(.+)").as_("query"))
async def bd_request(message: Message, query: re.Match):
    sql = query.group(1).strip()
    logging.info("bd: %s", sql)
    try:
        async with engine.begin() as conn:
            # exec_driver_sql, а не text(): иначе ':что-то' внутри строк парсится как параметр
            result = await conn.exec_driver_sql(sql)
            if result.returns_rows:
                rows = result.fetchmany(BD_MAX_ROWS + 1)
                lines = ["\t".join(result.keys())] + ["\t".join(map(str, row)) for row in rows[:BD_MAX_ROWS]]
                if len(rows) > BD_MAX_ROWS:
                    lines.append(f"… показаны первые {BD_MAX_ROWS} строк")
                answer = "\n".join(lines)
            else:
                answer = f"Готово, затронуто строк: {result.rowcount}" if result.rowcount >= 0 else "Готово"
    except Exception as e:
        answer = f"Ошибка: {getattr(e, 'orig', None) or e}"

    if len(answer) > 4000:
        await message.answer_document(BufferedInputFile(answer.encode(), filename="result.tsv"))
    else:
        await message.answer(f"<pre>{html.escape(answer)}</pre>")


SOURCE_TIMEOUT = 6


# API Сбера подписан корневым сертификатом Минцифры, которого нет в системном хранилище
GIGACHAT_SSL = ssl.create_default_context()
GIGACHAT_SSL.load_verify_locations("certs/russian_trusted_root_ca.pem")
GIGACHAT_PROMPT = (
    "Переведи английское слово или выражение на русский. Если это идиома или устойчивое выражение, "
    "переводи по смыслу, а не дословно. Ответь только вариантами перевода через запятую, "
    "от одного до трёх, строчными буквами, без пояснений."
)
_gigachat_token = {"value": "", "expires_at": 0.0}
_gigachat_lock = asyncio.Lock()  # на бесплатном тарифе генерация идёт в один поток


async def _from_gigachat(word: str) -> list[str]:
    if not GIGACHAT_AUTH_KEY:
        return []

    timeout = aiohttp.ClientTimeout(total=SOURCE_TIMEOUT)
    async with _gigachat_lock, aiohttp.ClientSession(timeout=timeout) as http_session:
        if time.time() > _gigachat_token["expires_at"] - 60:  # токен живёт 30 минут
            async with http_session.post(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers={"Authorization": f"Basic {GIGACHAT_AUTH_KEY}", "RqUID": str(uuid.uuid4())},
                data={"scope": "GIGACHAT_API_PERS"},
                ssl=GIGACHAT_SSL,
            ) as response:
                response.raise_for_status()
                token = await response.json()
            _gigachat_token.update(value=token["access_token"], expires_at=token["expires_at"] / 1000)

        async with http_session.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {_gigachat_token['value']}"},
            json={
                "model": "GigaChat-2",
                "temperature": 0.1,
                "max_tokens": 60,
                "messages": [
                    {"role": "system", "content": GIGACHAT_PROMPT},
                    {"role": "user", "content": word},
                ],
            },
            ssl=GIGACHAT_SSL,
        ) as response:
            if response.status == 401:
                _gigachat_token["expires_at"] = 0  # токен отозвали — в следующий раз получим новый
            response.raise_for_status()
            data = await response.json()

    answer = data["choices"][0]["message"]["content"]
    return [t.strip(" .\"'«»").lower() for t in answer.split(",") if t.strip(" .\"'«»")]


async def _from_yandex(word: str) -> list[str]:
    url = "https://dictionary.yandex.net/api/v1/dicservice.json/lookup"
    params = {
        "key": YANDEX_DICT_KEY or "",
        "lang": "en-ru",
        "text": word
    }

    timeout = aiohttp.ClientTimeout(total=SOURCE_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with http_session.get(url, params=params) as response:
            response.raise_for_status()
            data = await response.json()

    found = []
    for pos_block in data.get('def', []):
        for tr in pos_block.get('tr', []):
            found.append(tr['text'].lower())
            for syn in tr.get('syn', []):
                found.append(syn['text'].lower())
    return found


EXAMPLES_COUNT = 5
EXAMPLES_TIMEOUT = 12  # Tatoeba отвечает за 1–2 с, запросов бывает два, а параллельно сервер их не ускоряет


async def _from_tatoeba(word: str) -> list[str]:
    params = {
        "lang": "eng",
        "q": '"' + word.replace('"', "") + '"',
        "word_count": "6-18",  # короткие вроде «Level Two.» контекста не дают
        "is_unapproved": "no",
        "sort": "random",
        "limit": "20",
    }
    # поиск режет слово по дефисам и т. п. — оставляем только предложения, где оно действительно есть
    pattern = None if " " in word else re.compile(r"(?<![\w-])" + re.escape(word), re.IGNORECASE)

    sentences: list[str] = []
    timeout = aiohttp.ClientTimeout(total=EXAMPLES_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        # лучше всего предложения носителей, сразу написанные по-английски; их мало — добираем любыми проверенными
        for query in ({**params, "is_native": "yes", "origin": "original", "is_orphan": "no"}, params):
            async with http_session.get("https://api.tatoeba.org/v1/sentences", params=query) as response:
                response.raise_for_status()
                found = [sentence["text"] for sentence in (await response.json())["data"]]
            sentences = list(dict.fromkeys(sentences + [s for s in found if pattern is None or pattern.search(s)]))
            if len(sentences) >= EXAMPLES_COUNT:
                break
    return sentences[:EXAMPLES_COUNT]


async def _guarded(name: str, coro, timeout: float = SOURCE_TIMEOUT) -> list[str]:
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout)
        logging.info("%s: %d вариантов за %.1f с", name, len(result), time.monotonic() - started)
        return result
    except asyncio.TimeoutError:
        logging.warning("%s: таймаут после %.1f с", name, time.monotonic() - started)
    except Exception as e:
        logging.warning("%s: ошибка за %.1f с — %s", name, time.monotonic() - started, e)
    return []


def word_card(en_word: str, translations: list[str], examples: list[str], hidden: bool) -> str:
    shown = html.escape(", ".join(translations))
    text = f"🇬🇧 <b>{html.escape(en_word)}</b>\n🇷🇺 " + (f"<tg-spoiler>{shown}</tg-spoiler>" if hidden else shown)
    if examples:
        text += "\n<blockquote expandable>" + html.escape("\n".join(f"• {e}" for e in examples)) + "</blockquote>"
    return text


async def translate_word(word: str) -> list[str]:
    if " " in word:
        # фразы и идиомы словарь почти не знает — основной перевод от GigaChat
        gigachat, yandex = await asyncio.gather(
            _guarded("gigachat", _from_gigachat(word)),
            _guarded("yandex", _from_yandex(word)),
        )
        translations = gigachat + yandex
    else:
        translations = await _guarded("yandex", _from_yandex(word)) or await _guarded("gigachat", _from_gigachat(word))

    translations = list(dict.fromkeys(translations))
    return translations[:5] or ["перевод_не_найден"]


@dp.message(F.text)
async def add_word(message: Message):
    assert message.text is not None and message.from_user is not None
    en_word = message.text.strip().lower()
    # примеры ищутся несколько секунд — перевод отправляем сразу, а их дописываем, когда придут
    examples_task = asyncio.create_task(_guarded("tatoeba", _from_tatoeba(en_word), EXAMPLES_TIMEOUT))

    async with async_session() as session:
        known = (await session.execute(
            select(Word).where(
                Word.user_id == message.from_user.id,
                Word.word_en == en_word
            )
        )).scalars().first()
        if known is not None:
            known_id = known.id
            known_translations = list(known.translations_ru)
            known_saved_at = known.saved_at
            known_next = known.next_repeat_time
            known_idx = known.interval_index

    if known is not None:
        status = (
            f"\n\nУже в словаре с {known_saved_at:%d.%m.%Y}. "
            f"Следующее повторение {known_next:%d.%m в %H:%M}, интервал {INTERVALS_STR[known_idx]}."
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Удалить из базы", callback_data=DelWord(id=known_id).pack())
        ]])
        msg = await message.answer(word_card(en_word, known_translations, [], hidden=True) + status, reply_markup=markup)
        if (examples := await examples_task) and await word_exists(known_id):
            await msg.edit_text(word_card(en_word, known_translations, examples, hidden=True) + status, reply_markup=markup)
        return

    translations = await translate_word(en_word)

    msg = await message.answer(
        word_card(en_word, translations, [], hidden=False),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Удалить из базы", callback_data=DelWord(id=0).pack())
        ]]) if translations != ["перевод_не_найден"] else None
    )

    if translations == ["перевод_не_найден"]:
        examples_task.cancel()
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

        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Удалить из базы", callback_data=DelWord(id=new_word.id).pack())
        ]])
        await msg.edit_reply_markup(reply_markup=markup)
        await session.commit()

    # пока искались примеры, слово могли успеть удалить — тогда сообщение не трогаем
    if (examples := await examples_task) and await word_exists(new_word.id):
        await msg.edit_text(word_card(en_word, translations, examples, hidden=False), reply_markup=markup)

    await asyncio.sleep(30)
    if await word_exists(new_word.id):
        await msg.edit_text(word_card(en_word, translations, examples, hidden=True), reply_markup=markup)


async def word_exists(word_id: int) -> bool:
    async with async_session() as session:
        return await session.get(Word, word_id) is not None


@dp.callback_query(DelWord.filter())
async def del_word(query: CallbackQuery, callback_data: DelWord):
    async with async_session() as session:
        word = await session.get(Word, callback_data.id)
        if word:
            # из базы, а не из текста сообщения: там теперь примеры, и разметку текст не сохраняет
            card = html.escape(f"🇬🇧 {word.word_en}\n🇷🇺 {', '.join(word.translations_ru)}")
            await session.delete(word)
            await session.commit()
            assert isinstance(query.message, Message)
            await query.message.edit_text(f"<s>{card}</s>\nУдалено из базы.")
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

                text = (
                    f"<b>{word.word_en}</b>\n"
                    f"<tg-spoiler>{', '.join(word.translations_ru)}</tg-spoiler>\n\n"
                    f"Повторение слова через {INTERVALS_STR[max(next_idx - 1, 0)]}. Следующее -- через {INTERVALS_STR[next_idx]}"
                )
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Не вспомнил", callback_data=ForgotWord(id=word.id).pack())
                ]])
                try:
                    sent = await bot.send_message(
                        chat_id=word.user_id, text=text, reply_markup=markup,
                        reply_parameters=ReplyParameters(message_id=word.message_id, allow_sending_without_reply=True)
                    )
                except Exception as e:
                    logging.warning("повторение %r (id=%d) не отправлено: %s", word.word_en, word.id, e)
                    continue

                # у восстановленных из экспорта слов message_id со стороны пользователя, бот его не видит —
                # дальше отвечаем на только что отправленное повторение
                if sent.reply_to_message is None:
                    word.message_id = sent.message_id
                word.interval_index = next_idx
                word.next_repeat_time = now + INTERVALS[next_idx]
            if words:
                await session.commit()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
