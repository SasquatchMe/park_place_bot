"""Telegram bot that fills the Park Place "Material Move-In/Out" form on the fly.

The bot uses a single editable "card" message that updates in place as the user
fills in fields, instead of producing a long ladder of messages.

Run with::

    python bot.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
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
)

from fill_form import Category, FormValues, MoveType, fill_form

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("park_place_bot")


# ---------- configuration ----------------------------------------------------


def _read_env() -> tuple[str, set[int], Path]:
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

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━"


# ---------- per-user persisted defaults --------------------------------------


_DEFAULTABLE_FIELDS: tuple[str, ...] = (
    "unit",
    "company",
    "person_full_name",
    "tel",
)


def _load_user_defaults(user_id: int) -> dict[str, str]:
    if not DEFAULTS_FILE.exists():
        return {}
    try:
        payload = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read defaults file %s: %s", DEFAULTS_FILE, exc)
        return {}
    return payload.get(str(user_id), {})


def _save_user_defaults(user_id: int, defaults: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, dict[str, str]] = {}
    if DEFAULTS_FILE.exists():
        try:
            payload = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    cleaned = {key: defaults.get(key, "") for key in _DEFAULTABLE_FIELDS}
    payload[str(user_id)] = cleaned
    DEFAULTS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- FSM states -------------------------------------------------------


class FormFlow(StatesGroup):
    """States of the form-filling conversation."""

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
    confirm = State()
    edit_menu = State()
    done = State()


_STATE_BY_FIELD: dict[str, State] = {
    "unit": FormFlow.unit,
    "company": FormFlow.company,
    "person_full_name": FormFlow.person,
    "tel": FormFlow.tel,
    "date_str": FormFlow.date_str,
    "move_type": FormFlow.move_type,
    "category": FormFlow.category,
    "description": FormFlow.description,
    "quantity": FormFlow.quantity,
    "reason": FormFlow.reason,
}


# Order in which fields are asked (and shown on the card).
_FIELD_ORDER: tuple[str, ...] = (
    "unit",
    "company",
    "person_full_name",
    "tel",
    "date_str",
    "move_type",
    "category",
    "description",
    "quantity",
)
_TOTAL_STEPS = len(_FIELD_ORDER)


_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "unit": ("🏢", "Помещение"),
    "company": ("🏛", "Компания"),
    "person_full_name": ("👤", "ФИО"),
    "tel": ("📱", "Телефон"),
    "date_str": ("📅", "Дата"),
    "move_type": ("🔄", "Тип"),
    "category": ("📦", "Категория"),
    "description": ("📝", "Описание"),
    "quantity": ("🔢", "Количество"),
    "reason": ("❗", "Причина"),
}


_FIELD_PROMPTS: dict[str, str] = {
    "unit": "Введи номер помещения (например, <code>D1202</code>)",
    "company": "Название компании",
    "person_full_name": "ФИО ответственного лица",
    "tel": "Телефон (например, <code>+7 999 815-82-16</code>)",
    "date_str": "Дата (формат <code>ДД.ММ.ГГГГ</code>)",
    "move_type": "Внос или вынос?",
    "category": "Категория имущества",
    "description": "Описание (например, <code>Телевизор Tubio 65\"</code>)",
    "quantity": "Количество (целое положительное число)",
    "reason": "Причина (вынос &gt; 10 предметов — обязательно)",
}


_CATEGORY_LABELS: dict[str, str] = {
    "Personal Items": "👜 Личные вещи",
    "Equipment": "💻 Оборудование",
    "Furniture": "🪑 Мебель",
    "Other": "📦 Другое",
}


# ---------- card rendering ---------------------------------------------------


def _format_value(field: str, value: Any) -> str:
    """Pretty-format a stored value for display on the card."""
    if value is None or value == "":
        return "—"
    if field == "move_type":
        return "📥 Внос" if value == "IN" else "📤 Вынос"
    if field == "category":
        return _CATEGORY_LABELS.get(value, str(value))
    return str(value)


def _build_card_text(
    data: dict[str, Any],
    *,
    current_field: str | None = None,
    header: str = "📋 <b>Заявка на внос/вынос имущества</b>",
    footer_lines: list[str] | None = None,
    extra_message: str | None = None,
) -> str:
    """Build the multi-line text shown on the editable card."""
    lines: list[str] = [header, DIVIDER]

    has_filled = False
    for field in _FIELD_ORDER:
        value = data.get(field)
        if value in (None, ""):
            continue
        has_filled = True
        _, label = _FIELD_LABELS[field]
        lines.append(f"✅ <b>{label}:</b> {_format_value(field, value)}")

    reason_value = data.get("reason")
    if reason_value:
        _, label = _FIELD_LABELS["reason"]
        lines.append(f"✅ <b>{label}:</b> {reason_value}")
        has_filled = True

    if not has_filled and current_field is not None:
        # Keep the card from looking empty while we still ask the first question.
        lines.append("<i>(пока ничего не заполнено)</i>")

    if current_field is not None:
        emoji, label = _FIELD_LABELS[current_field]
        step_num = (
            _FIELD_ORDER.index(current_field) + 1
            if current_field in _FIELD_ORDER
            else _TOTAL_STEPS
        )
        lines.append(DIVIDER)
        if current_field == "reason":
            lines.append(f"<b>⚠️ {emoji} {label}</b>")
        else:
            lines.append(f"<b>Шаг {step_num}/{_TOTAL_STEPS} · {emoji} {label}</b>")
        lines.append(_FIELD_PROMPTS[current_field])

    if extra_message:
        lines.append("")
        lines.append(extra_message)

    if footer_lines:
        lines.append(DIVIDER)
        lines.extend(footer_lines)

    return "\n".join(lines)


# ---------- keyboards --------------------------------------------------------


def _step_keyboard(field: str, data: dict[str, Any]) -> InlineKeyboardMarkup:
    """Inline keyboard to render under the card for the given step."""
    rows: list[list[InlineKeyboardButton]] = []
    defaults = data.get("_defaults", {})

    if field in _DEFAULTABLE_FIELDS and defaults.get(field):
        saved = defaults[field]
        label = saved if len(saved) <= 50 else saved[:47] + "…"
        rows.append([InlineKeyboardButton(text=f"✓ {label}", callback_data=f"use:{field}")])

    if field == "date_str":
        today = date.today().strftime("%d.%m.%Y")
        rows.append(
            [InlineKeyboardButton(text=f"📅 Сегодня ({today})", callback_data="date:today")]
        )
    elif field == "move_type":
        rows.append(
            [
                InlineKeyboardButton(text="📥 Внос", callback_data="move:IN"),
                InlineKeyboardButton(text="📤 Вынос", callback_data="move:OUT"),
            ]
        )
    elif field == "category":
        rows.append(
            [
                InlineKeyboardButton(text="👜 Личные вещи", callback_data="cat:Personal Items"),
                InlineKeyboardButton(text="💻 Оборудование", callback_data="cat:Equipment"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="🪑 Мебель", callback_data="cat:Furniture"),
                InlineKeyboardButton(text="📦 Другое", callback_data="cat:Other"),
            ]
        )

    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сформировать форму", callback_data="confirm:ok")],
            [InlineKeyboardButton(text="✏️ Изменить поле", callback_data="confirm:edit")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
        ]
    )


def _edit_menu_keyboard(data: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for field in _FIELD_ORDER:
        emoji, label = _FIELD_LABELS[field]
        value = _format_value(field, data.get(field))
        button_text = f"{emoji} {label}: {value}"
        if len(button_text) > 60:
            button_text = button_text[:57] + "…"
        rows.append([InlineKeyboardButton(text=button_text, callback_data=f"edit:{field}")])
    if data.get("reason"):
        emoji, label = _FIELD_LABELS["reason"]
        rows.append(
            [InlineKeyboardButton(text=f"{emoji} {label}", callback_data="edit:reason")]
        )
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="edit:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🆕 Новая заявка", callback_data="restart")],
        ]
    )


# ---------- card update helpers ---------------------------------------------


async def _update_card(
    bot: Bot,
    state: FSMContext,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    """Edit the existing card; fall back to sending a new one if needed."""
    data = await state.get_data()
    card_id = data.get("_card_id")
    chat_id = data.get("_chat_id")
    if card_id and chat_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=card_id,
                text=text,
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("edit_message_text failed: %s", exc)
    if chat_id is None:
        return
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    await state.update_data(_card_id=sent.message_id, _chat_id=sent.chat.id)


async def _send_initial_card(
    message: Message, state: FSMContext, text: str, keyboard: InlineKeyboardMarkup | None
) -> None:
    sent = await message.answer(text, reply_markup=keyboard)
    await state.update_data(_card_id=sent.message_id, _chat_id=sent.chat.id)


async def _render_step(bot: Bot, state: FSMContext, field: str) -> None:
    """Render the card prompting for ``field`` and switch FSM state."""
    data = await state.get_data()
    text = _build_card_text(data, current_field=field)
    keyboard = _step_keyboard(field, data)
    await _update_card(bot, state, text, keyboard)
    await state.set_state(_STATE_BY_FIELD[field])


async def _render_confirmation(bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    text = _build_card_text(
        data,
        header="📋 <b>Подтверждение заявки</b>",
        footer_lines=["<b>Всё верно?</b>"],
    )
    await _update_card(bot, state, text, _confirm_keyboard())
    await state.set_state(FormFlow.confirm)


async def _render_edit_menu(bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    text = _build_card_text(
        data,
        header="✏️ <b>Какое поле изменить?</b>",
    )
    await _update_card(bot, state, text, _edit_menu_keyboard(data))
    await state.set_state(FormFlow.edit_menu)


async def _render_done(bot: Bot, state: FSMContext) -> None:
    text = (
        "✅ <b>Файл готов</b>\n"
        f"{DIVIDER}\n"
        "Скачай его выше. Чтобы заполнить ещё одну — кнопка ниже или /start"
    )
    await _update_card(bot, state, text, _done_keyboard())
    await state.set_state(FormFlow.done)


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
    await _start_new_form(message, state)


async def _start_new_form(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await state.clear()
    defaults = _load_user_defaults(user_id) if user_id else {}
    await state.update_data(_defaults=defaults)
    text = _build_card_text({"_defaults": defaults}, current_field="unit")
    keyboard = _step_keyboard("unit", {"_defaults": defaults})
    await _send_initial_card(message, state, text, keyboard)
    await state.set_state(FormFlow.unit)


@dispatcher.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. /start чтобы начать заново.")


@dispatcher.message(Command("clear_defaults"))
async def cmd_clear_defaults(message: Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id if message.from_user else None):
        return
    if message.from_user:
        _save_user_defaults(message.from_user.id, {})
    await message.answer("✅ Сохранённые значения сброшены.")


# ---------- value capture ---------------------------------------------------


async def _try_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def _accept_value(
    bot: Bot, state: FSMContext, field: str, value: Any
) -> None:
    """Persist a captured value and move to the next step (or back to confirm if editing)."""
    await state.update_data(**{field: value})
    data = await state.get_data()

    if data.get("_editing"):
        await state.update_data(_editing=False)
        await _render_confirmation(bot, state)
        return

    if field in _FIELD_ORDER:
        idx = _FIELD_ORDER.index(field)
        next_field = _FIELD_ORDER[idx + 1] if idx + 1 < len(_FIELD_ORDER) else None
    else:
        next_field = None

    if next_field is not None:
        await _render_step(bot, state, next_field)
        return

    # Walked past the last field — decide between reason and confirmation.
    needs_reason = (
        data.get("move_type") == "OUT"
        and int(data.get("quantity", 0)) > 10
        and not data.get("reason")
    )
    if needs_reason:
        text = _build_card_text(data, current_field="reason")
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]]
        )
        await _update_card(bot, state, text, keyboard)
        await state.set_state(FormFlow.reason)
    else:
        await _render_confirmation(bot, state)


# Text handlers --------------------------------------------------------------


@dispatcher.message(FormFlow.unit, F.text)
async def step_unit_text(message: Message, state: FSMContext) -> None:
    await _try_delete_message(message)
    await _accept_value(message.bot, state, "unit", message.text.strip())


@dispatcher.message(FormFlow.company, F.text)
async def step_company_text(message: Message, state: FSMContext) -> None:
    await _try_delete_message(message)
    await _accept_value(message.bot, state, "company", message.text.strip())


@dispatcher.message(FormFlow.person, F.text)
async def step_person_text(message: Message, state: FSMContext) -> None:
    await _try_delete_message(message)
    await _accept_value(message.bot, state, "person_full_name", message.text.strip())


@dispatcher.message(FormFlow.tel, F.text)
async def step_tel_text(message: Message, state: FSMContext) -> None:
    await _try_delete_message(message)
    await _accept_value(message.bot, state, "tel", message.text.strip())


@dispatcher.message(FormFlow.date_str, F.text)
async def step_date_text(message: Message, state: FSMContext) -> None:
    await _try_delete_message(message)
    await _accept_value(message.bot, state, "date_str", message.text.strip())


@dispatcher.message(FormFlow.description, F.text)
async def step_description_text(message: Message, state: FSMContext) -> None:
    await _try_delete_message(message)
    await _accept_value(message.bot, state, "description", message.text.strip())


@dispatcher.message(FormFlow.quantity, F.text)
async def step_quantity_text(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    await _try_delete_message(message)
    if not raw.isdigit() or int(raw) <= 0:
        data = await state.get_data()
        text = _build_card_text(
            data,
            current_field="quantity",
            extra_message="⚠️ Нужно положительное целое число.",
        )
        await _update_card(message.bot, state, text, _step_keyboard("quantity", data))
        return
    await _accept_value(message.bot, state, "quantity", int(raw))


@dispatcher.message(FormFlow.reason, F.text)
async def step_reason_text(message: Message, state: FSMContext) -> None:
    await _try_delete_message(message)
    await _accept_value(message.bot, state, "reason", message.text.strip())


# Default-value buttons ------------------------------------------------------


@dispatcher.callback_query(F.data.startswith("use:"))
async def cb_use_default(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    data = await state.get_data()
    saved = data.get("_defaults", {}).get(field)
    if not saved:
        await callback.answer("Нет сохранённого значения")
        return
    await callback.answer()
    await _accept_value(callback.message.bot, state, field, saved)


# Fixed-choice callbacks ------------------------------------------------------


@dispatcher.callback_query(FormFlow.date_str, F.data == "date:today")
async def cb_date_today(callback: CallbackQuery, state: FSMContext) -> None:
    today_str = date.today().strftime("%d.%m.%Y")
    await callback.answer()
    await _accept_value(callback.message.bot, state, "date_str", today_str)


@dispatcher.callback_query(FormFlow.move_type, F.data.startswith("move:"))
async def cb_move(callback: CallbackQuery, state: FSMContext) -> None:
    move_type = callback.data.split(":", 1)[1]
    await callback.answer()
    await _accept_value(callback.message.bot, state, "move_type", move_type)


@dispatcher.callback_query(FormFlow.category, F.data.startswith("cat:"))
async def cb_category(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.split(":", 1)[1]
    await callback.answer()
    await _accept_value(callback.message.bot, state, "category", cat)


# Confirmation ----------------------------------------------------------------


@dispatcher.callback_query(FormFlow.confirm, F.data == "confirm:ok")
async def cb_confirm_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Формирую файл…")
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
            reason=data.get("reason", ""),
        )
    except KeyError as missing:
        await callback.message.answer(f"Не хватает данных: {missing}. /start чтобы начать заново.")
        return

    document_bytes = await asyncio.get_running_loop().run_in_executor(
        None, fill_form, values
    )
    file_name = _build_file_name(values)
    document = BufferedInputFile(document_bytes, filename=file_name)

    await callback.message.answer_document(document)

    if values.move_type == "OUT":
        await callback.message.answer(
            "📌 На форме <b>Move-OUT</b> обязательно нужна "
            "<b>офисная печать</b> компании. Без неё пропуска не будет."
        )

    if callback.from_user:
        existing = _load_user_defaults(callback.from_user.id)
        existing.update(
            {
                "unit": values.unit,
                "company": values.company,
                "person_full_name": values.person_full_name,
                "tel": values.tel,
            }
        )
        _save_user_defaults(callback.from_user.id, existing)

    await state.update_data(
        _defaults={
            **data.get("_defaults", {}),
            "unit": values.unit,
            "company": values.company,
            "person_full_name": values.person_full_name,
            "tel": values.tel,
        },
    )
    await _render_done(callback.message.bot, state)


@dispatcher.callback_query(FormFlow.confirm, F.data == "confirm:edit")
async def cb_confirm_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _render_edit_menu(callback.message.bot, state)


# Edit menu -------------------------------------------------------------------


@dispatcher.callback_query(FormFlow.edit_menu, F.data.startswith("edit:"))
async def cb_edit_select(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "back":
        await callback.answer()
        await _render_confirmation(callback.message.bot, state)
        return
    if field not in _STATE_BY_FIELD:
        await callback.answer()
        return
    await state.update_data(_editing=True)
    await callback.answer("Введи новое значение")
    await _render_step(callback.message.bot, state, field)


# Restart / cancel ------------------------------------------------------------


@dispatcher.callback_query(F.data == "restart")
async def cb_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    user_id = callback.from_user.id if callback.from_user else 0
    defaults = _load_user_defaults(user_id) if user_id else {}
    await state.clear()
    await state.update_data(
        _defaults=defaults,
        _card_id=callback.message.message_id,
        _chat_id=callback.message.chat.id,
    )
    text = _build_card_text({"_defaults": defaults}, current_field="unit")
    keyboard = _step_keyboard("unit", {"_defaults": defaults})
    await _update_card(callback.message.bot, state, text, keyboard)
    await state.set_state(FormFlow.unit)


@dispatcher.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if callback.message is not None:
        try:
            await callback.message.edit_text(
                "❌ Отменено\n\n/start чтобы начать заново.",
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass


# ---------- file naming ------------------------------------------------------


def _build_file_name(values: FormValues) -> str:
    safe_unit = "".join(ch for ch in values.unit if ch.isalnum())
    safe_date = values.date_str.replace(".", "-").replace("/", "-")
    return f"IN_OUT_{values.move_type}_{safe_unit}_{safe_date}.docx"


# ---------- entry point ------------------------------------------------------


async def main() -> None:
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    logger.info(
        "Bot starting; whitelist=%s data_dir=%s",
        sorted(ALLOWED_USER_IDS),
        DATA_DIR,
    )
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
