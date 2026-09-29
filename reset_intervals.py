"""Возвращает восстановленным из экспорта словам интервалы из эталонной базы.

После восстановления планировщик не мог отправить им повторения (message_id
со стороны пользователя, бот его не видит), но всё равно сдвигал интервал.
Скрипт трогает только строки, у которых message_id всё ещё из экспорта:
после первой успешной отправки бот его перезаписывает.

Ещё не наступившие повторения остаются как в эталоне. Просроченные слова
равномерно раскладываются внутри своего интервала от текущего момента, чтобы
не прийти разом: каждое придёт не позже, чем через свой интервал. Короткие
интервалы растягиваются до --min-window часов.

Запускать при остановленном боте, внутри контейнера (чтобы время совпадало с ботом):
    docker compose run --rm angl-recognition python reset_intervals.py          # показать изменения
    docker compose run --rm angl-recognition python reset_intervals.py --apply  # записать
"""
import argparse
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from main import INTERVALS

FMT = "%Y-%m-%d %H:%M:%S.%f"
COLUMNS = "id, user_id, message_id, word_en, saved_at, interval_index, next_repeat_time"

parser = argparse.ArgumentParser()
parser.add_argument("--db", default="data/words.db")
parser.add_argument("--reference", default="data/restored.db", help="база, собранная из экспорта чата")
parser.add_argument("--min-window", type=int, default=24, help="минимальное окно раскладки в часах")
parser.add_argument("--apply", action="store_true", help="записать изменения (по умолчанию только показать)")
args = parser.parse_args()

ref = sqlite3.connect(f"file:{args.reference}?mode=ro", uri=True)
reference = {
    (id_, user_id, word_en, saved_at): (message_id, idx, next_time)
    for id_, user_id, message_id, word_en, saved_at, idx, next_time
    in ref.execute(f"select {COLUMNS} from words")
}

db = sqlite3.connect(args.db)
now = datetime.now()
keep, overdue = [], defaultdict(list)
for id_, user_id, message_id, word_en, saved_at, idx, next_time in db.execute(f"select {COLUMNS} from words"):
    ref_row = reference.get((id_, user_id, word_en, saved_at))
    if ref_row is None or ref_row[0] != message_id:
        continue  # слово добавлено после восстановления или бот уже отправил ему повторение
    _, ref_idx, ref_time = ref_row
    row = (ref_time, id_, word_en, idx, ref_idx, next_time)
    if datetime.strptime(ref_time, FMT) > now:
        keep.append((row, datetime.strptime(ref_time, FMT)))
    else:
        overdue[ref_idx].append(row)

planned = list(keep)
for ref_idx, rows in overdue.items():
    window = max(INTERVALS[ref_idx], timedelta(hours=args.min_window))
    for k, row in enumerate(sorted(rows)):
        planned.append((row, now + window * (k + 1) / len(rows)))

updates = []
print(f"сейчас {now:%d.%m %H:%M:%S}, к восстановлению {len(planned)} из {len(reference)} слов\n")
for (_, id_, word_en, old_idx, new_idx, old_time), new_time in sorted(planned, key=lambda p: p[1]):
    updates.append((new_idx, new_time.strftime(FMT), id_))
    print(f"{id_:>4} {word_en:<25} интервал {old_idx} -> {new_idx}   {old_time[:16]} -> {new_time:%Y-%m-%d %H:%M}")

per_day = Counter((new_time.date() - now.date()).days for _, new_time in planned)
print("\nповторений по дням от сегодня:", ", ".join(f"+{d}: {n}" for d, n in sorted(per_day.items())))

if not updates:
    print("нечего менять")
elif args.apply:
    backup_path = f"{args.db}.bak-{now:%Y%m%d-%H%M%S}"
    with sqlite3.connect(backup_path) as backup:
        db.backup(backup)
    with db:
        db.executemany("update words set interval_index = ?, next_repeat_time = ? where id = ?", updates)
    print(f"\nзаписано {len(updates)} строк, резервная копия: {backup_path}")
else:
    print("\nэто пробный запуск, для записи добавь --apply")
