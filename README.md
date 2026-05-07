# Park Place Move-In/Out Bot

Telegram-бот, заполняющий форму **Material Move-In / Out (Park Place)** на лету
и отдающий готовый `.docx`.

## Состав

- `bot.py` — бот (aiogram 3, FSM-диалог).
- `fill_form.py` — функция `fill_form(values) -> bytes`, заполняет шаблон.
- `template/form.docx` — исходный шаблон формы.
- `Dockerfile`, `docker-compose.yml` — для деплоя.

## Логика

Бот ведёт пошаговый диалог: помещение → компания → ФИО → телефон → дата →
тип (IN/OUT) → категория → описание → количество. Если выбран **Move-OUT и
количество > 10**, дополнительно спрашивает причину (обязательно). После
формирования файла для **Move-OUT** напоминает: нужна офисная печать.

Доступ ограничен Telegram user id из `ALLOWED_USER_IDS`.

## Запуск локально

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # и проставь BOT_TOKEN, ALLOWED_USER_IDS
python bot.py
```

## Деплой через Docker Compose

```bash
cp .env.example .env  # отредактируй
docker compose up -d --build
docker compose logs -f bot
```

## Переменные окружения

| Имя | Описание |
|-----|----------|
| `BOT_TOKEN` | Токен бота из @BotFather |
| `ALLOWED_USER_IDS` | Через запятую — Telegram user id, которым разрешено пользоваться ботом |
