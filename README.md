# XAUUSD SMC Signal Bot

Telegram-бот **паперових сигналів** XAUUSD за стратегією Smart Money Concepts
(M5/M15, kill zones Лондона і Нью-Йорка). Бот **не торгує**: лише читає котирування
і надсилає сигнали.

> Paper signal. Not financial advice.

## Статус

- [x] Фаза 1 — каркас (конфіг, логування JSON з ротацією, Docker, ruff/mypy/pytest)
- [x] Фаза 2 — дані (Twelve Data live, Dukascopy історія + parquet-кеш)
- [x] Фаза 3 — сесії (DST), години ринку, розмір позиції в лотах, R:R
- [x] Фаза 4 — стратегія SMC (swings, BOS/CHoCH, ліквідність і sweep, FVG, OB, premium/discount, сигнал з поясненням)
- [x] Фаза 5 — бектест (runner, звіт, walk-forward; результати в reports/)
- [x] Фаза 6 — Telegram (сигнали, оновлення відповіддю, команди, доступ лише адміну)
- [x] Фаза 7 — live-цикл, трекінг, heartbeat, денний звіт
- [x] Фаза 8 — деплой на Ubuntu VPS, healthcheck, бекап БД

---

## Розгортання на VPS з нуля (~15 хвилин)

### 0. Що знадобиться (≈5 хв)

| Що | Де взяти |
|---|---|
| **VPS** з Ubuntu 22.04 або 24.04, 1 vCPU / 1 GB RAM / 10 GB диска | Hetzner, DigitalOcean, Contabo тощо (≈4–6 $/міс) |
| **Ключ Twelve Data** | https://twelvedata.com → Sign up (безкоштовно) → Dashboard → API Keys |
| **Токен Telegram-бота** | у Telegram відкрийте **@BotFather** → `/newbot` → назва → ім'я з `bot` у кінці → скопіюйте токен вигляду `123456789:AA...` |
| **Ваш chat ID** | дізнаєтеся на кроці 4 (бот сам його покаже) |

### 1. Підключитися до сервера і встановити Docker (≈3 хв)

На своєму комп'ютері:

```bash
ssh root@IP_ВАШОГО_СЕРВЕРА
```

На сервері:

```bash
apt-get update && apt-get install -y git
```

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
docker --version && docker compose version
```

### 2. Завантажити код (≈1 хв)

```bash
git clone https://github.com/zhenyakertis-bot/xauusd-bot.git /opt/xauusd-bot
```

```bash
cd /opt/xauusd-bot && cp .env.example .env && mkdir -p data logs cache && chown -R 1000:1000 data logs cache
```

Репозиторій приватний? Тоді при `git clone` GitHub попросить логін і
**Personal Access Token** замість пароля (GitHub → Settings → Developer settings →
Personal access tokens → Fine-grained → доступ *Contents: Read* до цього репозиторію).

### 3. Заповнити `.env` (≈2 хв)

```bash
nano .env
```

Обов'язково:

```ini
TWELVEDATA_API_KEY=ваш_ключ_twelvedata
TELEGRAM_BOT_TOKEN=123456789:AA...        # без лапок і без < >
TELEGRAM_ADMIN_CHAT_ID=                   # поки порожньо, заповните на кроці 4
PAPER_BALANCE=10000
RISK_PERCENT=1.0
```

Збережіть: `Ctrl+O`, `Enter`, вийдіть: `Ctrl+X`.

### 4. Запустити і дізнатися chat ID (≈3 хв)

```bash
docker compose up -d --build
```

Відкрийте свого бота в Telegram і натисніть **Start**. Бот відповість:

> Your chat id is 123456789

Впишіть це число в `.env` (`TELEGRAM_ADMIN_CHAT_ID=123456789`) і перезапустіть:

```bash
docker compose up -d
```

У Telegram має прийти «Bot started…». Надішліть `/status` — бот покаже ринок,
сесію, останню ціну і bias M15.

### 5. Перевірити (≈1 хв)

```bash
docker compose ps
```

У колонці STATUS через ~2 хв має бути `healthy`.

```bash
docker compose logs -f --tail=50
```

Тестовий сигнал у ваш чат (демо-дані, позначені `[TEST]`):

```bash
docker compose exec bot python -m app.telegram.bot test-signal
```

Готово. Бот перезапускається сам після збою і після перезавантаження сервера
(`restart: unless-stopped`).

---

## Щоденна робота

### Команди в Telegram (лише для вашого chat ID)

| Команда | Що робить |
|---|---|
| `/status` | чи працює бот, ринок, сесія, остання ціна, bias M15, останній результат перевірки |
| `/active` | активні сигнали |
| `/last` | останні 5 сигналів з результатами |
| `/stats` | статистика за 7 і 30 днів: кількість, winrate, середній R, сума R, max drawdown |
| `/pause`, `/resume` | зупинити / відновити генерацію нових сигналів (трекінг відкритих триває) |
| `/settings` | поточні параметри стратегії |

Автоматично: сигнал → оновлення відповіддю (вхід, TP1 з порадою перенести SL у
беззбиток, TP2, SL, беззбиток, expired); денний звіт пн–пт о 11:30 за Нью-Йорком;
алерт, якщо в торговий час дані не оновлюються > 15 хв.

### Оновлення бота

```bash
cd /opt/xauusd-bot && git pull && docker compose up -d --build
```

Відкриті сигнали і статистика зберігаються в `data/bot.db` і переживають оновлення.

### Зміна параметрів стратегії

```bash
nano config/strategy.yaml
```

```bash
docker compose restart
```

### Логи

```bash
docker compose logs -f --tail=100
```

JSON-логи з ротацією також у `logs/bot.log` (10 МБ × 5 файлів).

### Бекапи

Бот щодня о 00:17 UTC робить копію бази в `data/backups/` (зберігаються останні 14).
Зробити копію вручну:

```bash
docker compose exec bot python -m app.storage.backup
```

Забрати копії на свій комп'ютер (виконати на своєму комп'ютері):

```bash
scp -r root@IP_ВАШОГО_СЕРВЕРА:/opt/xauusd-bot/data/backups ./xauusd-backups
```

Відновити з копії:

```bash
docker compose stop && cp data/backups/bot-РРРРММДД-ГГХХ.db data/bot.db && docker compose start
```

### Якщо щось не так

| Симптом | Що перевірити |
|---|---|
| Бот не відповідає в Telegram | `docker compose ps` (має бути `Up … (healthy)`); `docker compose logs --tail=100` |
| `InvalidToken` у логах | токен у `.env` без лапок і `< >`; токен не відкликано в @BotFather |
| «no TELEGRAM_ADMIN_CHAT_ID» | заповніть `TELEGRAM_ADMIN_CHAT_ID` і `docker compose up -d` |
| Бот відповідає лише «Your chat id is…» | chat ID у `.env` не збігається з вашим |
| `unhealthy` | цикл не виконується: дивіться логи на `cycle failed`; часто — неправильний ключ Twelve Data або вичерпаний ліміт 800 запитів/добу |
| Алерт «no new market data» | збій Twelve Data; бот сам надішле «flowing again», коли дані повернуться |
| Тиша кілька днів | норма: на поточних налаштуваннях (V3) в середньому ~1 сигнал на місяць; `/status` → «Last check» показує, на якому етапі відсіюється сетап |

Безпека: `.env` ніколи не комітьте; не публікуйте токени в чатах; якщо токен
засвітився — `/revoke` у @BotFather і новий токен у `.env`.

---

## Розробка

### Локальний запуск

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
```

```bash
cp .env.example .env && python -m app.main
```

Перевірки:

```bash
ruff check . && ruff format --check . && mypy app && pytest
```

Live-перевірка джерел даних (потрібна мережа і ключ): `pytest -m live`.

### Дані

| Провайдер | Призначення | Примітки |
|---|---|---|
| `twelvedata` | live-сигнали | безкоштовний ключ, ≤800 запитів/добу (бот використовує < 500), без bid/ask — спред з `trade.assumed_spread` |
| `dukascopy` | бектест | безкоштовно, M1 bid/ask, затримка до ~1 год, сервер обмежує частоту запитів (429) — завантажувач робить паузи |

Повні дні Dukascopy кешуються в `cache/` (parquet).

### Бектест

```bash
python -m app.backtest.data --from 2025-01-01 --to 2026-10-29
```

```bash
python -m app.backtest.runner --from 2025-01-06 --to 2026-10-01
```

Варіант «а що, якби» без зміни `strategy.yaml`:

```bash
python -m app.backtest.runner --from 2025-01-06 --to 2026-10-01 --set structure.choch_sets_bias=false
```

Результат у `reports/backtest_<from>_<to>[__overrides]/`: `report.md` (метрики,
walk-forward in-sample/out-of-sample, розбивка за сесіями, напрямком, днями тижня
і кварталами), `equity.png`, `trades.csv`, `summary.json`.

Правила симуляції: тільки закриті свічки; вхід лімітом по ask (BUY) / bid (SELL);
вихід по bid (BUY) / ask (SELL); SL і TP в одній хвилинній свічці — SL; на свічці
входу TP не зараховується; проковзування на стопах; після TP1 закривається
`tp1_close_fraction`, решта йде до TP2 зі стопом у беззбитку.

### Конфігурація

- `.env` — секрети і runtime (див. `.env.example`).
- `config/strategy.yaml` — усі параметри стратегії. Поточні значення — варіант **V3**
  з бектесту (CHoCH теж задає bias, без фільтра premium/discount, вікно sweep 240 хв,
  допуск equal highs/lows 1.0). Значення зі специфікації вказані в коментарях.
