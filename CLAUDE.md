# CLAUDE.md — XAUUSD SMC Signal Bot

Telegram-бот **паперових сигналів** XAUUSD (Smart Money Concepts, M5/M15, kill zones Лондона і Нью-Йорка).
Спілкування з власником — українською.

## Жорсткі правила
- **Тільки сигнали.** Жодного виконання угод, жодних order/trade endpoint'ів брокерів. Лише читання котирувань.
- **Без lookahead.** Рушій бачить тільки закриті свічки (`drop_incomplete`). Те саме в бектесті.
- Уся логіка стратегії — детерміновані правила з параметрами в `config/strategy.yaml`. Нічого «на око».
- Кожне повідомлення в Telegram закінчується рядком `Paper signal. Not financial advice.`
- Кожен сигнал має пояснення (`Signal.explanation`).
- **Секрети тільки в `.env` або змінних середовища** (`TWELVEDATA_API_KEY`, `TELEGRAM_BOT_TOKEN`). Ніколи не комітити, не логувати, не виводити в чат. Перед комітом перевіряти `git grep` на ключі й токени.
- **Не змінювати параметри стратегії без підтвердження власника.** Варіанти перевіряти через `--set` у бектесті, `strategy.yaml` не правити.

## Стек
Python 3.11+ (venv у `.venv`), asyncio, httpx, python-telegram-bot v22, pandas/numpy, pydantic-settings, SQLAlchemy 2 (SQLite), APScheduler 3, matplotlib, pytest/ruff/mypy (strict), Docker.

## Дані
- `twelvedata`: live. Безкоштовний ключ, ≤800 запитів/добу, без bid/ask (використовується `trade.assumed_spread`). Ключ передається в заголовку `Authorization`, не в URL.
- `dukascopy`: історія M1 bid/ask для бектесту. Місяці в URL нумеруються з 0. Кеш у `cache/` (parquet). Сервер обмежує частоту запитів (429), тому завантаження послідовне з паузами.
- OANDA **не використовується** (у власника немає рахунку).

## Структура
- `app/config.py`: `Settings` (.env) і `StrategyConfig` (yaml)
- `app/data/`: провайдери, `candles.py` (resample, `drop_incomplete`, `ParquetCache`)
- `app/sessions.py`: kill zones з урахуванням DST, години ринку золота (торговий день перемикається о 17:00 за Нью-Йорком)
- `app/risk/position_size.py`: лоти (1 лот = 100 oz), обчислення в Decimal, округлення завжди вниз
- `app/strategy/`: `structure.py` (swings, BOS/CHoCH, bias), `liquidity.py` (Asia, PDH/PDL, equal, sweep), `zones.py` (ATR, FVG, OB), `smc_engine.py` (`SMCEngine.evaluate` повертає `Evaluation`: сигнал або етап і причину відмови)
- `app/signals/`: `models.py`, `dedup.py` (`SignalGate`), `tracker.py` (`TradeTracker`, M1). Спільні для live і бектесту.
- `app/backtest/`: `data.py` (завантаження), `runner.py` (CLI, `--set section.key=value`), `report.py`
- `app/telegram/`: `bot.py` (команди лише для адміна; CLI `whoami`, `test-signal`, `run`), `formatter.py`, `demo.py`
- `app/live.py`: `LiveRunner.cycle(now)` з явним часом, щоб тестувати програванням
- `app/main.py`: APScheduler (цикл кожні 5 хв на :20 с; денний звіт; бекап о 00:17 UTC)
- `app/storage/`: `db.py` (SQLite), `backup.py`
- `app/healthcheck.py`: Docker HEALTHCHECK за свіжістю `data/health`

## Поточна стратегія — V3 (обрана власником)
Від специфікації відрізняється так: `choch_sets_bias: true`, `require_premium_discount: false`, `eq_tolerance: 1.0`, `sweep_max_age_minutes: 240`.
Бектест за 21 міс.: 17 сигналів, 5 входів, +14.8R. **Вибірка статистично замала.** Заплановано перепрацювати правила й перевіряти кожну зміну через walk-forward.
Тести рушія фіксують правила специфікації (`SPEC_RULES` у `tests/test_smc_engine.py`), щоб зміни yaml не ламали відомі відповіді.

## Команди
```bash
. .venv/bin/activate
ruff check . && ruff format --check . && mypy app && pytest     # перед кожним комітом
pytest -m live                                                  # живі API
python -m app.backtest.runner --from 2025-01-06 --to 2026-10-01 [--set structure.choch_sets_bias=true]
python -m app.telegram.bot whoami | test-signal
python -m app.main
docker compose up -d --build
```

## Конвенції
- Довжина рядка 100; `ruff` дозволяє `–` і `×` (використовуються в повідомленнях).
- Нова логіка = тест на синтетичних свічках з відомою відповіддю (`tests/synthetic.py`: `bars`, `path_bars`).
- Час завжди tz-aware UTC; локальний час — тільки для відображення.
- Консервативні правила трекера: SL і TP в одній свічці — це SL; TP на свічці входу не зараховується; на SL додається проковзування.

## Репозиторій
GitHub: `zhenyakertis-bot/xauusd-bot`, гілка `main`. Деплой — Ubuntu VPS + Docker (Фаза 8).
