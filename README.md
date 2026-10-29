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
- [x] Фаза 7 — live-цикл, трекінг, heartbeat, денний звіт (24-год прогін — на VPS)
- [ ] Фаза 8 — деплой (Ubuntu VPS)

## Швидкий старт (локально)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env   # заповніть секрети
python -m app.main
```

## Docker

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

## Перевірки

```bash
ruff check . && ruff format --check . && mypy app && pytest
```

## Дані

| Провайдер | Призначення | Примітки |
|---|---|---|
| `twelvedata` | live-сигнали | безкоштовний ключ на twelvedata.com, ≤800 запитів/добу, без bid/ask (спред — параметр) |
| `dukascopy` | бектест | безкоштовно, M1 bid/ask, затримка до ~1 год, обмеження частоти запитів (сервер відповідає 429) |

Повні дні Dukascopy кешуються в `cache/` (parquet), тому повторні завантаження миттєві.

Live-перевірка джерел (потрібна мережа і ключ):

```bash
pytest -m live
```

## Бектест

```bash
# 1) завантажити історію Dukascopy в кеш (перший раз ~1 год на 22 місяці)
python -m app.backtest.data --from 2025-01-01 --to 2026-10-29
# 2) прогнати той самий рушій, що й live, свічка за свічкою
python -m app.backtest.runner --from 2025-01-01 --to 2026-10-01
```

Результат у `reports/backtest_<from>_<to>/`: `report.md` (метрики, walk-forward
in-sample/out-of-sample, розбивка за сесіями, днями тижня і кварталами), `equity.png`,
`trades.csv`, `summary.json`.

Правила симуляції: тільки закриті свічки; вхід лімітом по ask (BUY) / bid (SELL);
вихід по bid (BUY) / ask (SELL); SL і TP в одній хвилинній свічці рахуються як SL;
на свічці входу TP не зараховується; проковзування на стопах; після TP1 закривається
`tp1_close_fraction`, решта йде до TP2 зі стопом у беззбитку.

## Конфігурація

- `.env` — секрети і runtime (див. `.env.example`). Ніколи не комітьте `.env`.
- `config/strategy.yaml` — усі параметри стратегії.
