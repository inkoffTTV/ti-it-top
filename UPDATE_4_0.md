# TI:IT TOP 4.0 — update notes

## Что изменено

1. Полный переработанный Mini App UI.
2. Новый tournament detail экран и визуальная 16-team playoff сетка.
3. Hybrid seeding: seed #1 и #2 выбираются админом, #3–#16 распределяются по среднему подтверждённому MMR.
4. Состояние сетки хранится в `runtime/brackets/`, SQLite схема не меняется.
5. Убран расчёт матчей из часов Dota 2: значения без реального источника больше не генерируются.
6. Исправлено сопоставление team ID ↔ seed на публичной странице турнира.
7. После подачи заявки страница турнира обновляет свои данные, а не показывает устаревший статус.

## Важное для обновления

Архив обновления **намеренно не содержит `tournament.db`**.

Не используй `unzip -o ...` поверх проекта с опцией удаления файлов и не копируй в live-каталог архивную БД. Текущую БД нужно оставить на месте.

Также не удаляй `runtime/brackets/*.json`: в них может находиться уже зафиксированная playoff-сетка.

## Безопасное обновление на VPS

```bash
cd /opt/ti-it-top/work310

# 1. Бэкап БД
cp -a tournament.db "tournament.db.BEFORE_4_0_$(date +%Y%m%d_%H%M%S)"

# 2. Бэкап кода без live-БД и пользовательских аватаров
cd /opt/ti-it-top
tar --exclude='work310/tournament.db*' \
     --exclude='work310/miniapp/data/team_avatars/*' \
     -czf "work310_code.BEFORE_4_0_$(date +%Y%m%d_%H%M%S).tar.gz" work310
cd /opt/ti-it-top/work310

# 3. Остановить сервис на время замены файлов
systemctl stop ti-it-top.service

# 4. Распаковать архив во временный каталог
rm -rf /tmp/ti-it-top-4.0
mkdir -p /tmp/ti-it-top-4.0
unzip -q TI_IT_TOP_4_0_UPDATE.zip -d /tmp/ti-it-top-4.0

# 5. Скопировать код. tournament.db в архиве НЕТ, поэтому live-БД не трогается.
cp -a /tmp/ti-it-top-4.0/. /opt/ti-it-top/work310/

# 6. Проверить Python-синтаксис до запуска
cd /opt/ti-it-top/work310
.venv/bin/python -m py_compile services/bracket.py services/player_verification.py miniapp/api.py

# 7. Запустить
systemctl start ti-it-top.service
systemctl is-active ti-it-top.service

# 8. Проверить последние логи
journalctl -u ti-it-top.service -n 80 --no-pager

# 9. Проверить SQLite
sqlite3 /opt/ti-it-top/work310/tournament.db 'PRAGMA integrity_check;'
```

## Что должно получиться

- `systemctl is-active ti-it-top.service` → `active`
- `PRAGMA integrity_check;` → `ok`
- в логах нет traceback при старте
- Mini App открывается с новой версией UI
- текущие команды, пользователи, заявки и проверки остаются в существующей БД

## Поведение сетки

Для турнира на 16 команд администратор выбирает две команды вручную:

- seed #1 — выбранная команда;
- seed #2 — выбранная команда.

Остальные 14 команд сортируются по среднему подтверждённому MMR. Затем используется стандартная сетка первого раунда:

```text
#1  vs #16
#8  vs #9
#4  vs #13
#5  vs #12
#2  vs #15
#7  vs #10
#3  vs #14
#6  vs #11
```

До фиксации сетку можно перегенерировать. Для фиксации каждая команда должна иметь минимум 5 подтверждённых значений MMR.
