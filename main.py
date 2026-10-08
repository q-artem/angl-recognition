import aiohttp
import asyncio
import html
import logging
import os
import random
import re
import ssl
import struct
import time
import uuid
import zlib
from dotenv import load_dotenv
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InaccessibleMessage, ReplyParameters, BufferedInputFile
from aiogram.filters.callback_data import CallbackData
from aiogram.exceptions import TelegramBadRequest
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
    examples: Mapped[list | None] = mapped_column(JSON, nullable=True)  # до EXAMPLES_STORED предложений из Tatoeba
    saved_at: Mapped[datetime] = mapped_column(default=datetime.now)
    next_repeat_time: Mapped[datetime]
    interval_index: Mapped[int] = mapped_column(default=0)


class DelWord(CallbackData, prefix="del"):
    id: int


class ForgotWord(CallbackData, prefix="fgt"):
    id: int


class KeepOriginal(CallbackData, prefix="keep"):
    id: int  # исправленное слово, добавленное в словарь этой карточкой; 0 — удалять нечего
    word: str


class SwitchMode(CallbackData, prefix="mode"):
    id: int  # 0 — слово ещё не в словаре (не нашлось в прежнем режиме)
    to_dict: bool


session = AiohttpSession(proxy=PROXY_URL)


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode='HTML'), session=session)
logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

from aiogram.filters import CommandStart, Command, CommandObject

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


async def _from_yandex(word: str, lang: str = "en-ru") -> list[str]:
    url = "https://dictionary.yandex.net/api/v1/dicservice.json/lookup"
    params = {
        "key": YANDEX_DICT_KEY or "",
        "lang": lang,
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


EXAMPLES_COUNT = 5  # показываем случайные из сохранённых, чтобы в повторениях они менялись
EXAMPLES_STORED = 10
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
            if len(sentences) >= EXAMPLES_STORED:
                break
    return sentences[:EXAMPLES_STORED]


def pick_examples(examples: list[str] | None) -> list[str]:
    examples = examples or []
    return random.sample(examples, min(EXAMPLES_COUNT, len(examples)))


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


def is_russian(word: str) -> bool:
    return re.search(r"[а-яё]", word, re.IGNORECASE) is not None


def flags(word: str) -> tuple[str, str]:
    # русские слова лежат в тех же колонках word_en / translations_ru, просто направление перевода обратное
    return ("🇷🇺", "🇬🇧") if is_russian(word) else ("🇬🇧", "🇷🇺")


NOT_FOUND = "перевод_не_найден"
DICT_PATH = "data/ru-ru_teddy-20151106"  # сводный толковый словарь в формате StarDict
DICT_MARK = "__dict_definition__"  # в translations_ru вместо перевода: слово сохранено в режиме толкования
DICT_NOT_FOUND = "нет в словаре"
DICT_ARTICLES = 2  # сколько статей показывать в карточке и повторении
MESSAGE_LIMIT = 4096  # лимит Bot API на текст сообщения
dict_mode: dict[int, bool] = {}  # последний выбранный пользователем режим для русских слов


class RuDictionary:
    """StarDict: индекс держим в памяти, статьи читаем из .dict.dz кусками, не распаковывая файл целиком."""

    def __init__(self, path: str):
        self.articles: dict[str, list[tuple[int, int]]] = {}
        idx = open(path + ".idx", "rb").read()
        i = 0
        while i < len(idx):
            end = idx.index(b"\0", i)
            offset, size = struct.unpack(">II", idx[end + 1:end + 9])
            self.articles.setdefault(idx[i:end].decode(), []).append((offset, size))
            i = end + 9

        # dictzip — это gzip, у которого в поле FEXTRA лежит таблица независимо сжатых кусков
        self.file = open(path + ".dict.dz", "rb")
        flags = self.file.read(10)[3]
        extra = self.file.read(struct.unpack("<H", self.file.read(2))[0])
        self.chunk_len, chunk_count = struct.unpack("<HH", extra[6:10])
        chunk_sizes = struct.unpack(f"<{chunk_count}H", extra[10:10 + 2 * chunk_count])
        for flag in (8, 16):  # имя файла и комментарий, оба до нулевого байта
            if flags & flag:
                while self.file.read(1) != b"\0":
                    pass
        if flags & 2:
            self.file.read(2)
        self.chunks = [self.file.tell()]
        for size in chunk_sizes:
            self.chunks.append(self.chunks[-1] + size)

    def _read(self, offset: int, size: int) -> str:
        first, last = offset // self.chunk_len, (offset + size - 1) // self.chunk_len
        data = b""
        for chunk in range(first, last + 1):
            self.file.seek(self.chunks[chunk])
            data += zlib.decompressobj(-15).decompress(self.file.read(self.chunks[chunk + 1] - self.chunks[chunk]))
        start = offset - first * self.chunk_len
        return data[start:start + size].decode("utf-8", "replace")

    def lookup(self, word: str, limit: int | None = None) -> list[tuple[str, str]]:
        """Статьи по порядку: (название словаря, текст без разметки)."""
        entries = self.articles.get(word) or self.articles.get(word.capitalize()) or []
        return [self._article(self._read(offset, size)) for offset, size in entries[:limit]]

    @staticmethod
    def _article(raw: str) -> tuple[str, str]:
        source = re.search(r"<sup>(.*?)</sup>", raw)
        raw = re.sub(r"<script.*?</script>|<link[^>]*>|<div class=\"k\">.*?</div>|<sup>.*?</sup>", "", raw, flags=re.S)
        text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", raw)))
        lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
        return (html.unescape(source.group(1)) if source else "", "\n".join(line for line in lines if line))


try:
    RU_DICT = RuDictionary(DICT_PATH)
except OSError as e:
    RU_DICT = None
    logging.warning("толковый словарь не загружен: %s", e)


def definition_quote(word: str) -> str:
    """Первые статьи словаря свёрнутой цитатой; обрезаем, чтобы карточка влезла в сообщение."""
    articles = RU_DICT.lookup(word, DICT_ARTICLES) if RU_DICT else []
    if not articles:
        return ""
    text = "\n\n".join(f"{source}\n{body}" for source, body in articles)
    budget = MESSAGE_LIMIT - 300 - len(word)  # 300 — запас на заголовок карточки и строку статуса
    if len(text) > budget:
        text = text[:budget].rstrip() + "…"
    return (
        f"\n<blockquote expandable>{html.escape(text)}\n\n"
        f"<code>/dict {html.escape(word)}</code> — все толкования</blockquote>"
    )


def word_card(word: str, translations: list[str], examples: list[str], hidden: bool, corrected_from: str | None = None) -> str:
    source_flag, target_flag = flags(word)
    text = f"{source_flag} <b>{html.escape(word)}</b>"
    if translations == [DICT_NOT_FOUND]:
        text += f"\n📖 {DICT_NOT_FOUND}"
    elif translations != [DICT_MARK]:
        shown = html.escape(", ".join(translations))
        text += f"\n{target_flag} " + (f"<tg-spoiler>{shown}</tg-spoiler>" if hidden else shown)
    if corrected_from:
        text += f"\n<i>исправлено: {html.escape(corrected_from)} → {html.escape(word)}</i>"
    if translations == [DICT_MARK]:
        return text + definition_quote(word)
    return text + examples_quote(examples)


async def correct_spelling(text: str) -> str:
    try:
        timeout = aiohttp.ClientTimeout(total=SOURCE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            async with http_session.get(
                "https://speller.yandex.net/services/spellservice.json/checkText",
                params={"text": text, "lang": "ru" if is_russian(text) else "en"},
            ) as response:
                response.raise_for_status()
                errors = await response.json()
    except Exception as e:
        logging.warning("speller: ошибка — %s", e)
        return text

    # правим с конца, чтобы позиции ещё не исправленных слов не съезжали
    for error in sorted(errors, key=lambda e: e["pos"], reverse=True):
        if error["s"]:
            text = text[:error["pos"]] + error["s"][0] + text[error["pos"] + error["len"]:]
    return text


def examples_quote(examples: list[str]) -> str:
    if not examples:
        return ""
    return "\n<blockquote expandable>" + html.escape("\n".join(f"• {e}" for e in examples)) + "</blockquote>"


async def translate_word(word: str) -> list[str]:
    if is_russian(word):
        translations = await _guarded("yandex", _from_yandex(word, "ru-en"))
    elif " " in word:
        # фразы и идиомы словарь почти не знает — основной перевод от GigaChat
        gigachat, yandex = await asyncio.gather(
            _guarded("gigachat", _from_gigachat(word)),
            _guarded("yandex", _from_yandex(word)),
        )
        translations = gigachat + yandex
    else:
        translations = await _guarded("yandex", _from_yandex(word)) or await _guarded("gigachat", _from_gigachat(word))

    translations = list(dict.fromkeys(translations))
    return translations[:5] or [NOT_FOUND]


@dp.message(Command("dict"))
async def dict_command(message: Message, command: CommandObject):
    word = (command.args or "").strip().lower()
    articles = RU_DICT.lookup(word) if RU_DICT and word else []
    if not articles:
        await message.answer("Нет в словаре" if word else "Напиши слово после команды: <code>/dict гуталин</code>")
        return
    # markdown склеивает соседние строки, поэтому в конце каждой строки жёсткий перенос — два пробела
    body = "\n\n".join(f"## {source}\n\n" + "  \n".join(text.splitlines()) for source, text in articles)
    await message.answer_document(BufferedInputFile(f"# {word}\n\n{body}\n".encode(), filename=f"{word}_толкования.md"))


@dp.message(F.text)
async def add_word(message: Message):
    assert message.text is not None and message.from_user is not None
    await handle_word(message, message.from_user.id, message.text, autocorrect=True)


async def handle_word(chat: Message, user_id: int, text: str, autocorrect: bool):
    """Переводит слово и добавляет в словарь; chat — любое сообщение из нужного чата, ответ уходит туда."""
    original = text.strip().lower()
    word = (await correct_spelling(original)).lower() if autocorrect else original
    corrected_from = original if word != original else None

    async with async_session() as session:
        known = (await session.execute(
            select(Word).where(
                Word.user_id == user_id,
                Word.word_en == word
            )
        )).scalars().first()
        if known is not None:
            known_id = known.id
            known_translations = list(known.translations_ru)
            known_saved_at = known.saved_at
            known_next = known.next_repeat_time
            known_idx = known.interval_index
            known_examples = known.examples

    # примеры ищутся несколько секунд — перевод отправляем сразу, а их дописываем, когда придут;
    # для русских слов примеров нет, у известного слова они могут быть уже сохранены
    examples_task = asyncio.create_task(
        asyncio.sleep(0, result=[]) if is_russian(word) or (known is not None and known_examples)
        else _guarded("tatoeba", _from_tatoeba(word), EXAMPLES_TIMEOUT)
    )

    if known is not None:
        status = (
            f"\n\nУже в словаре с {known_saved_at:%d.%m.%Y}. "
            f"Следующее повторение {known_next:%d.%m в %H:%M}, интервал {INTERVALS_STR[known_idx]}."
        )
        markup = card_markup(known_id, corrected_from, new_word=False, in_dict=russian_mode(word, known_translations))
        shown = pick_examples(known_examples)
        msg = await chat.answer(word_card(word, known_translations, shown, True, corrected_from) + status, reply_markup=markup)
        if not known_examples and (examples := await examples_task) and await store_examples(known_id, examples):
            shown = pick_examples(examples)
            await msg.edit_text(word_card(word, known_translations, shown, True, corrected_from) + status, reply_markup=markup)
        return

    if is_russian(word) and dict_mode.get(user_id):
        translations = [DICT_MARK] if RU_DICT and RU_DICT.lookup(word, 1) else [DICT_NOT_FOUND]
    else:
        translations = await translate_word(word)
    found = translations not in ([NOT_FOUND], [DICT_NOT_FOUND])
    in_dict = russian_mode(word, translations)

    msg = await chat.answer(
        word_card(word, translations, [], False, corrected_from),
        reply_markup=card_markup(0 if found else None, corrected_from, new_word=True, in_dict=in_dict)
    )

    if not found:
        examples_task.cancel()
        return

    async with async_session() as session:
        new_word = Word(
            user_id=user_id,
            message_id=msg.message_id,
            word_en=word,
            translations_ru=translations,
            next_repeat_time=datetime.now() + INTERVALS[0],
            interval_index=0
        )
        session.add(new_word)
        await session.flush()

        markup = card_markup(new_word.id, corrected_from, new_word=True, in_dict=in_dict)
        await msg.edit_reply_markup(reply_markup=markup)
        await session.commit()

    # пока искались примеры, слово могли успеть удалить — тогда сообщение не трогаем
    shown = []
    if (examples := await examples_task) and await store_examples(new_word.id, examples):
        shown = pick_examples(examples)
        await msg.edit_text(word_card(word, translations, shown, False, corrected_from), reply_markup=markup)

    await asyncio.sleep(30)
    if await word_exists(new_word.id):
        await msg.edit_text(word_card(word, translations, shown, True, corrected_from), reply_markup=markup)


def russian_mode(word: str, translations: list[str]) -> bool | None:
    """Для русских слов — показано ли толкование (иначе перевод); для английских None."""
    return translations in ([DICT_MARK], [DICT_NOT_FOUND]) if is_russian(word) else None


def card_markup(
    word_id: int | None, corrected_from: str | None, new_word: bool, in_dict: bool | None = None
) -> InlineKeyboardMarkup | None:
    rows = []
    if word_id is not None:
        rows.append([InlineKeyboardButton(text="Удалить из базы", callback_data=DelWord(id=word_id).pack())])
    if corrected_from:
        try:
            # исходное слово храним в самой кнопке, чтобы она работала и после перезапуска бота;
            # уже известное слово отмена удалять не должна, поэтому для него id = 0
            data = KeepOriginal(id=word_id if new_word and word_id else 0, word=corrected_from).pack()
            rows.append([InlineKeyboardButton(text=f"Оставить {corrected_from}", callback_data=data)])
        except ValueError:
            pass  # длиннее 64 байт или с «:» — в кнопку не влезает
    if in_dict is not None:
        rows.append([InlineKeyboardButton(
            text="Перевести" if in_dict else "Найти в словаре",
            callback_data=SwitchMode(id=word_id or 0, to_dict=not in_dict).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def word_exists(word_id: int) -> bool:
    async with async_session() as session:
        return await session.get(Word, word_id) is not None


async def store_examples(word_id: int, examples: list[str]) -> bool:
    """Сохраняет примеры слова; False, если слово уже удалили."""
    async with async_session() as session:
        word = await session.get(Word, word_id)
        if word is None:
            return False
        word.examples = examples
        await session.commit()
        return True


@dp.callback_query(DelWord.filter())
async def del_word(query: CallbackQuery, callback_data: DelWord):
    async with async_session() as session:
        word = await session.get(Word, callback_data.id)
        if word:
            # из базы, а не из текста сообщения: там теперь примеры, и разметку текст не сохраняет
            source_flag, target_flag = flags(word.word_en)
            meaning = "📖 толкование" if word.translations_ru == [DICT_MARK] else f"{target_flag} {', '.join(word.translations_ru)}"
            card = html.escape(f"{source_flag} {word.word_en}\n{meaning}")
            await session.delete(word)
            await session.commit()
            assert isinstance(query.message, Message)
            await query.message.edit_text(f"<s>{card}</s>\nУдалено из базы.")
        else:
            await query.answer("Слово не найдено в базе.")


@dp.callback_query(KeepOriginal.filter())
async def keep_original(query: CallbackQuery, callback_data: KeepOriginal):
    assert isinstance(query.message, Message)
    if callback_data.id:
        # исправленное слово попало в словарь только из-за этой карточки — убираем его
        async with async_session() as session:
            word = await session.get(Word, callback_data.id)
            if word:
                await session.delete(word)
                await session.commit()
    await query.answer()
    try:
        await query.message.delete()
    except TelegramBadRequest:  # старше 48 часов — удалить уже нельзя, просто убираем кнопки
        await query.message.edit_reply_markup(reply_markup=None)
    await handle_word(query.message, query.from_user.id, callback_data.word, autocorrect=False)


@dp.callback_query(SwitchMode.filter())
async def switch_mode(query: CallbackQuery, callback_data: SwitchMode):
    assert isinstance(query.message, Message) and query.message.text is not None
    word = query.message.text.split("\n", 1)[0].split(" ", 1)[1]  # первая строка карточки — «🇷🇺 слово»
    if callback_data.to_dict:
        translations = [DICT_MARK] if RU_DICT and RU_DICT.lookup(word, 1) else None
    else:
        translations = await translate_word(word)
        translations = None if translations == [NOT_FOUND] else translations
    if translations is None:
        await query.answer(DICT_NOT_FOUND.capitalize() if callback_data.to_dict else "Перевод не найден", show_alert=True)
        return

    dict_mode[query.from_user.id] = callback_data.to_dict
    async with async_session() as session:
        row = await session.get(Word, callback_data.id) if callback_data.id else None
        if row is None:  # в прежнем режиме слово не нашлось и в словарь не попало — добавляем сейчас
            row = Word(
                user_id=query.from_user.id,
                message_id=query.message.message_id,
                word_en=word,
                next_repeat_time=datetime.now() + INTERVALS[0],
                interval_index=0,
            )
            session.add(row)
        row.translations_ru = translations
        await session.commit()
        word_id = row.id

    await query.answer()
    await query.message.edit_text(
        word_card(word, translations, [], hidden=False),
        reply_markup=card_markup(word_id, None, new_word=True, in_dict=callback_data.to_dict),
    )


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

                # у старых слов примеров ещё нет — ищем один раз и сохраняем
                if not word.examples and not is_russian(word.word_en):
                    word.examples = await _guarded("tatoeba", _from_tatoeba(word.word_en), EXAMPLES_TIMEOUT) or None
                examples = pick_examples(word.examples)
                meaning = (
                    definition_quote(word.word_en) if word.translations_ru == [DICT_MARK]
                    else f"\n<tg-spoiler>{', '.join(word.translations_ru)}</tg-spoiler>{examples_quote(examples)}"
                )
                text = (
                    f"<b>{word.word_en}</b>{meaning}\n\n"
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
