"""Telegram bot that fills the Park Place "Material Move-In/Out" form on the fly.

Run with::

    python bot.py

Required env vars (see ``.env.example``)::

    BOT_TOKEN          — Telegram Bot API token from @BotFather.
    ALLOWED_USER_IDS   — comma-separated whitelist of Telegram user ids.
    DATA_DIR           — optional, where per-user defaults are persisted
                         (defaults to ``/app/data``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from fill_form import Category, FormValues, MoveType, fill_form

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("park_place_bot")


# ---------- configuration ----------------------------------------------------


def _read_env() -> tuple[str, set[int], Path]:
    """Read required env vars; raise on missing values.

    :return: tuple of (bot token, set of allowed Telegram user ids, data directory).
    """
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is not set")
    raw_users = os.environ.get("ALLOWED_USER_IDS", "").strip()
    if not raw_users:
        raise SystemExit("ALLOWED_USER_IDS is not set (comma-separated Telegram user ids)")
    allowed = {int(part) for part in raw_users.split(",") if part.strip()}
    data_dir = Path(os.environ.get("DATA_DIR", "/app/data"))
    return token, allowed, data_dir


BOT_TOKEN, ALLOWED_USER_IDS, DATA_DIR = _read_env()
DEFAULTS_FILE = DATA_DIR / "defaults.json"


# ---------- per-user persisted defaults --------------------------------------


# Fields that we remember between sessions (the rarely-changing identity bits).
_DEFAULTABLE_FIELDS: tuple[str, ...] = ("unit", "company", "person_full_name", "tel")


def _load_user_defaults(user_id: int) -> dict[str, str]:
    """Read the saved defaults for the given Telegram user id.

    Returns an empty dict when nothing is saved yet, or when the file is unreadable.
    """
    if not DEFAULTS_FILE.exists():
        return {}
    try:
        payload = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read defaults file %s: %s", DEFAULTS_FILE, exc)
        return {}
    return payload.get(str(user_id), {})


def _save_user_defaults(user_id: int, defaults: dict[str, str]) -> None:
    """Persist the defaults for the given Telegram user id (merging with anything saved)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, dict[str, str]] = {}
    if DEFAULTS_FILE.exists():
        try:
            payload = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    payload[str(user_id)] = {key: defaults.get(key, "") for key in _DEFAULTABLE_FIELDS}
    DEFAULTS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- FSM states -------------------------------------------------------


class FormFlow(StatesGroup):
    """Each state corresponds to a single question the bot asks the user."""

    unit = State()
    company = State()
    person = State()
    tel = State()
    date_str = State()
    move_type = State()
    category = State()
    description = State()
    quantity = State()
    reason = State()


# ---------- keyboards --------------------------------------------------------


def _move_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📥 Внос (IN)", callback_data="move:IN"),
                InlineKeyboardButton(text="📤 Вынос (OUT)", callback_data="move:OUT"),
            ],
        ],
    )


def _category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👜 Личные вещи", callback_data="cat:Personal Items"),
                InlineKeyboardButton(text="💻 Оборудование", callback_data="cat:Equipment"),
            ],
            [
                InlineKeyboardButton(text="🪑 Мебель", callback_data="cat:Furniture"),
                InlineKeyboardButton(text="📦 Другое", callback_data="cat:Other"),
            ],
        ],
    )


def _today_keyboard() -> InlineKeyboardMarkup:
    today_str = date.today().strftime("%d.%m.%Y")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📅 Сегодня ({today_str})", callback_data="date:today")],
        ],
    )


def _default_keyboard(field: str, value: str) -> InlineKeyboardMarkup:
    """Inline keyboard with a single «keep saved value» button for a defaultable field."""
    label = value if len(value) <= 60 else value[:57] + "…"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✓ {label}", callback_data=f"use:{field}")],
        ],
    )


# ---------- access control ---------------------------------------------------


def _is_allowed(user_id: int | None) -> bool:
    return user_id is not None and user_id in ALLOWED_USER_IDS


# ---------- dispatcher and handlers -----------------------------------------

dispatcher = Dispatcher(storage=MemoryStorage())


@dispatcher.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id if message.from_user else None):
        await message.answer("⛔ Доступ запрещён.")
        return
    await state.clear()
    defaults = _load_user_defaults(message.from_user.id) if message.from_user else {}
    await state.update_data(_defaults=defaults)

    intro = "Привет! Заполняем форму на внос/вынос имущества (Park Place).\n\n"
    await message.answer(intro, reply_markup=ReplyKeyboardRemove())
    await _ask_unit(message, state, defaults)
    await state.set_state(FormFlow.unit)


@dispatcher.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. Чтобы начать заново — /start.")


@dispatcher.message(Command("clear_defaults"))
async def cmd_clear_defaults(message: Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id if message.from_user else None):
        return
    if message.from_user:
        _save_user_defaults(message.from_user.id, {})
    await message.answer("✅ Сохранённые значения сброшены.")


# ---------- step prompts -----------------------------------------------------


async def _ask_unit(message: Message, state: FSMContext, defaults: dict[str, str]) -> None:
    text = "Введи <b>номер помещения</b> (например, <code>D1202</code>):"
    keyboard = _default_keyboard("unit", defaults["unit"]) if defaults.get("unit") else None
    await message.answer(text, reply_markup=keyboard)


async def _ask_company(message: Message, defaults: dict[str, str]) -> None:
    keyboard = _default_keyboard("company", defaults["company"]) if defaults.get("company") else None
    await message.answer("<b>Компания:</b>", reply_markup=keyboard)


async def _ask_person(message: Message, defaults: dict[str, str]) -> None:
    keyboard = (
        _default_keyboard("person_full_name", defaults["person_full_name"])
        if defaults.get("person_full_name")
        else None
    )
    await message.answer("<b>ФИО ответственного лица:</b>", reply_markup=keyboard)


async def _ask_tel(message: Message, defaults: dict[str, str]) -> None:
    keyboard = _default_keyboard("tel", defaults["tel"]) if defaults.get("tel") else None
    await message.answer(
        "<b>Телефон</b> (например, <code>+7 999 815-82-16</code>):",
        reply_markup=keyboard,
    )


async def _ask_date(message: Message) -> None:
    await message.answer(
        "<b>Дата</b> (формат <code>ДД.ММ.ГГГГ</code>) — или нажми кнопку:",
        reply_markup=_today_keyboard(),
    )


async def _ask_move_type(message: Message) -> None:
    await message.answer("Это <b>внос</b> или <b>вынос</b>?", reply_markup=_move_type_keyboard())


# ---------- step transitions (text + default-button branches) ---------------


async def _accept_unit(message: Message, state: FSMContext, value: str) -> None:
    await state.update_data(unit=value)
    data = await state.get_data()
    await _ask_company(message, data.get("_defaults", {}))
    await state.set_state(FormFlow.company)


async def _accept_company(message: Message, state: FSMContext, value: str) -> None:
    await state.update_data(company=value)
    data = await state.get_data()
    await _ask_person(message, data.get("_defaults", {}))
    await state.set_state(FormFlow.person)


async def _accept_person(message: Message, state: FSMContext, value: str) -> None:
    await state.update_data(person_full_name=value)
    data = await state.get_data()
    await _ask_tel(message, data.get("_defaults", {}))
    await state.set_state(FormFlow.tel)


async def _accept_tel(message: Message, state: FSMContext, value: str) -> None:
    await state.update_data(tel=value)
    await _ask_date(message)
    await state.set_state(FormFlow.date_str)


# Text handlers --------------------------------------------------------------


@dispatcher.message(FormFlow.unit, F.text)
async def step_unit(message: Message, state: FSMContext) -> None:
    await _accept_unit(message, state, message.text.strip())


@dispatcher.message(FormFlow.company, F.text)
async def step_company(message: Message, state: FSMContext) -> None:
    await _accept_company(message, state, message.text.strip())


@dispatcher.message(FormFlow.person, F.text)
async def step_person(message: Message, state: FSMContext) -> None:
    await _accept_person(message, state, message.text.strip())


@dispatcher.message(FormFlow.tel, F.text)
async def step_tel(message: Message, state: FSMContext) -> None:
    await _accept_tel(message, state, message.text.strip())


# Default-button handlers ----------------------------------------------------


@dispatcher.callback_query(FormFlow.unit, F.data == "use:unit")
async def step_unit_default(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    saved = data.get("_defaults", {}).get("unit", "")
    await _strip_kb(callback)
    if callback.message:
        await _accept_unit(callback.message, state, saved)
    await callback.answer()


@dispatcher.callback_query(FormFlow.company, F.data == "use:company")
async def step_company_default(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    saved = data.get("_defaults", {}).get("company", "")
    await _strip_kb(callback)
    if callback.message:
        await _accept_company(callback.message, state, saved)
    await callback.answer()


@dispatcher.callback_query(FormFlow.person, F.data == "use:person_full_name")
async def step_person_default(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    saved = data.get("_defaults", {}).get("person_full_name", "")
    await _strip_kb(callback)
    if callback.message:
        await _accept_person(callback.message, state, saved)
    await callback.answer()


@dispatcher.callback_query(FormFlow.tel, F.data == "use:tel")
async def step_tel_default(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    saved = data.get("_defaults", {}).get("tel", "")
    await _strip_kb(callback)
    if callback.message:
        await _accept_tel(callback.message, state, saved)
    await callback.answer()


# Date / move type / category / description / qty / reason -------------------


@dispatcher.callback_query(FormFlow.date_str, F.data == "date:today")
async def step_date_today(callback: CallbackQuery, state: FSMContext) -> None:
    today_str = date.today().strftime("%d.%m.%Y")
    await state.update_data(date_str=today_str)
    await _strip_kb(callback)
    if callback.message:
        await _ask_move_type(callback.message)
    await state.set_state(FormFlow.move_type)
    await callback.answer()


@dispatcher.message(FormFlow.date_str, F.text)
async def step_date_text(message: Message, state: FSMContext) -> None:
    await state.update_data(date_str=message.text.strip())
    await _ask_move_type(message)
    await state.set_state(FormFlow.move_type)


@dispatcher.callback_query(FormFlow.move_type, F.data.startswith("move:"))
async def step_move_type(callback: CallbackQuery, state: FSMContext) -> None:
    move_type: MoveType = callback.data.split(":", 1)[1]  # type: ignore[assignment]
    await state.update_data(move_type=move_type)
    await _strip_kb(callback)
    if callback.message:
        await callback.message.answer(
            "Выбери <b>категорию</b>:", reply_markup=_category_keyboard()
        )
    await state.set_state(FormFlow.category)
    await callback.answer()


@dispatcher.callback_query(FormFlow.category, F.data.startswith("cat:"))
async def step_category(callback: CallbackQuery, state: FSMContext) -> None:
    category: Category = callback.data.split(":", 1)[1]  # type: ignore[assignment]
    await state.update_data(category=category)
    await _strip_kb(callback)
    if callback.message:
        await callback.message.answer(
            "Опиши имущество одной строкой "
            "(например: <code>Телевизор Tubio 65\"</code>):"
        )
    await state.set_state(FormFlow.description)
    await callback.answer()


@dispatcher.message(FormFlow.description, F.text)
async def step_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description=message.text.strip())
    await message.answer("<b>Количество</b> (число):")
    await state.set_state(FormFlow.quantity)


@dispatcher.message(FormFlow.quantity, F.text)
async def step_quantity(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Нужно положительное целое число. Попробуй ещё раз.")
        return
    quantity = int(text)
    await state.update_data(quantity=quantity)
    data = await state.get_data()
    if data.get("move_type") == "OUT" and quantity > 10:
        await message.answer(
            "⚠️ Это <b>вынос</b> и предметов больше 10 — обязательно укажи "
            "<b>причину</b>:"
        )
        await state.set_state(FormFlow.reason)
        return
    await _finalize(message, state, reason="")


@dispatcher.message(FormFlow.reason, F.text)
async def step_reason(message: Message, state: FSMContext) -> None:
    await _finalize(message, state, reason=message.text.strip())


# ---------- helpers ----------------------------------------------------------


async def _strip_kb(callback: CallbackQuery) -> None:
    """Remove the inline keyboard from the message that fired the callback."""
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 - best effort, ignore "not modified" etc.
            pass


async def _finalize(message: Message, state: FSMContext, *, reason: str) -> None:
    data = await state.get_data()
    try:
        values = FormValues(
            unit=data["unit"],
            company=data["company"],
            person_full_name=data["person_full_name"],
            tel=data["tel"],
            date_str=data["date_str"],
            move_type=data["move_type"],
            category=data["category"],
            description=data["description"],
            quantity=int(data["quantity"]),
            reason=reason,
        )
    except KeyError as missing:
        await message.answer(f"Не хватает данных: {missing}. Начни заново — /start.")
        await state.clear()
        return

    try:
        document_bytes = await asyncio.get_running_loop().run_in_executor(
            None, fill_form, values
        )
    except ValueError as exc:
        await message.answer(f"Ошибка: {exc}")
        return

    file_name = _build_file_name(values)
    document = BufferedInputFile(document_bytes, filename=file_name)

    summary = (
        f"📋 <b>Форма заполнена</b>\n"
        f"• Помещение: <code>{values.unit}</code>\n"
        f"• Компания: {values.company}\n"
        f"• ФИО: {values.person_full_name}\n"
        f"• Дата: {values.date_str}\n"
        f"• Тип: {'Внос' if values.move_type == 'IN' else 'Вынос'}\n"
        f"• Категория: {values.category}\n"
        f"• Описание: {values.description}\n"
        f"• Количество: {values.quantity}"
    )
    if values.reason:
        summary += f"\n• Причина: {values.reason}"
    await message.answer_document(document, caption=summary)

    if values.move_type == "OUT":
        await message.answer(
            "📌 <b>Напоминание</b>: на форме <b>Move-OUT</b> обязательно нужна "
            "<b>офисная печать</b> компании. Без неё пропуска не будет."
        )

    # Persist defaults for the next session
    if message.from_user:
        _save_user_defaults(
            message.from_user.id,
            {
                "unit": values.unit,
                "company": values.company,
                "person_full_name": values.person_full_name,
                "tel": values.tel,
            },
        )

    await state.clear()
    await message.answer("Готово. Чтобы заполнить ещё одну — /start.")


def _build_file_name(values: FormValues) -> str:
    safe_unit = "".join(ch for ch in values.unit if ch.isalnum())
    safe_date = values.date_str.replace(".", "-").replace("/", "-")
    return f"IN_OUT_{values.move_type}_{safe_unit}_{safe_date}.docx"


# ---------- entry point ------------------------------------------------------


async def main() -> None:
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    logger.info(
        "Bot starting; whitelist=%s data_dir=%s", sorted(ALLOWED_USER_IDS), DATA_DIR
    )
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
