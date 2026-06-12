import io
import os
import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import anthropic
import openai
import notion_helper

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
# Groq expone la misma API que OpenAI, solo cambia la base_url
openai_client = openai.OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)

IDLE = "idle"
WAITING_NEW_CAT = "waiting_new_cat"

user_states: dict[int, dict] = {}
conversation_history: dict[int, list[dict]] = {}

URL_REGEX = re.compile(r"https?://\S+|www\.\S+")


def get_state(chat_id: int) -> dict:
    return user_states.get(chat_id, {"state": IDLE})


def set_state(chat_id: int, state: str, **data) -> None:
    user_states[chat_id] = {"state": state, **data}


def clear_state(chat_id: int) -> None:
    user_states.pop(chat_id, None)


def classify_message(text: str) -> str:
    """Returns 'note' or 'question'."""
    if URL_REGEX.search(text):
        return "note"
    response = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        system=(
            "Classify this message as 'note' (something to save: a reminder, thought, "
            "piece of info) or 'question' (asking for help, info, or assistance). "
            "Reply only: note or question"
        ),
        messages=[{"role": "user", "content": text}],
    )
    result = response.content[0].text.strip().lower()
    return "note" if "note" in result else "question"


def build_category_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"cat:{cat}")]
        for cat in categories
    ]
    buttons.append([InlineKeyboardButton("➕ Nueva categoría", callback_data="cat:__new__")])
    return InlineKeyboardMarkup(buttons)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hola! Podés enviarme:\n"
        "• 🎙️ Notas de voz → las transcribo y proceso\n"
        "• 🔗 Links, 📷 fotos o 📝 texto → los guardo en Notion por categoría\n"
        "• ❓ Preguntas → te respondo usando tus notas guardadas como contexto\n\n"
        "Comandos:\n"
        "/notas — ver resumen de categorías guardadas\n"
        "/reset — borrar historial de conversación"
    )


async def cmd_notas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        categories = notion_helper.get_categories()
        if not categories:
            await update.message.reply_text("No tenés notas guardadas todavía.")
            return
        lines = ["📚 *Categorías guardadas:*\n"]
        for cat in categories:
            notes = notion_helper.get_notes_by_category(cat)
            lines.append(f"• *{cat}* — {len(notes)} nota(s)")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error("Error al leer categorías: %s", e)
        await update.message.reply_text("Error al leer las notas. Revisá la configuración de Notion.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conversation_history.pop(chat_id, None)
    clear_state(chat_id)
    await update.message.reply_text("Historial de conversación borrado.")


async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if state["state"] != "waiting_cat":
        return

    if query.data == "cat:__new__":
        set_state(chat_id, WAITING_NEW_CAT, pending_note=state["pending_note"])
        await query.edit_message_text("¿Cómo querés llamar a la nueva categoría?")
        return

    category = query.data.replace("cat:", "", 1)
    await _save_note_and_reply(query.edit_message_text, state["pending_note"], category)
    clear_state(chat_id)


async def _save_note_and_reply(reply_fn, note: dict, category: str) -> None:
    try:
        notion_helper.add_note(
            content=note["content"],
            category=category,
            note_type=note["type"],
            url=note.get("url"),
        )
        await reply_fn(f'✅ Guardado en *{category}*.', parse_mode="Markdown")
    except Exception as e:
        logger.error("Error al guardar en Notion: %s", e)
        await reply_fn("Error al guardar en Notion. Revisá la configuración.")


async def _ask_category(message, note: dict, chat_id: int) -> None:
    categories = notion_helper.get_categories()
    keyboard = build_category_keyboard(categories)
    set_state(chat_id, "waiting_cat", pending_note=note)
    prompt = "¿En qué categoría guardamos esto?" if categories else "Primera nota. ¿En qué categoría la guardamos?"
    await message.reply_text(prompt, reply_markup=keyboard)


async def _process_text_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    chat_id: int,
) -> None:
    """Core logic shared by text messages and transcribed voice notes."""
    state = get_state(chat_id)

    if state["state"] == WAITING_NEW_CAT:
        category = text.strip()
        await _save_note_and_reply(update.message.reply_text, state["pending_note"], category)
        clear_state(chat_id)
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    intent = classify_message(text)

    if intent == "note":
        is_link = bool(URL_REGEX.search(text))
        match = URL_REGEX.search(text) if is_link else None
        note = {
            "content": text,
            "type": "link" if is_link else "texto",
            "url": match.group(0) if match else None,
        }
        await _ask_category(update.message, note, chat_id)
    else:
        await _answer_with_context(update, context, text, chat_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _process_text_input(update, context, update.message.text, update.effective_chat.id)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        audio_bytes = await voice_file.download_as_bytearray()

        audio_buffer = io.BytesIO(bytes(audio_bytes))
        audio_buffer.name = "voice.ogg"

        transcript = openai_client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=audio_buffer,
        )
        text = transcript.text.strip()
    except Exception as e:
        logger.error("Error al transcribir audio: %s", e)
        await update.message.reply_text("No pude transcribir el audio. Intentá de nuevo.")
        return

    await update.message.reply_text(f"🎙️ _{text}_", parse_mode="Markdown")
    await _process_text_input(update, context, text, chat_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    caption = update.message.caption or "Foto sin descripción"
    note = {"content": caption, "type": "foto", "url": file.file_path}
    await _ask_category(update.message, note, chat_id)


async def _answer_with_context(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    chat_id: int,
) -> None:
    notes_context = ""
    try:
        all_notes = notion_helper.get_all_notes()
        if all_notes:
            lines = ["Notas personales del usuario guardadas en Notion:"]
            for n in all_notes:
                line = f"[{n['category']}] ({n['type']}) {n['content']}"
                if n["url"]:
                    line += f" → {n['url']}"
                lines.append(line)
            notes_context = "\n".join(lines)
    except Exception as e:
        logger.error("Error al leer notas de Notion: %s", e)

    system_prompt = (
        "Sos un asistente personal. Respondé en el mismo idioma que el usuario, "
        "de forma concisa y útil."
    )
    if notes_context:
        system_prompt += f"\n\nTenés acceso a las notas personales del usuario:\n{notes_context}"

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({"role": "user", "content": text})
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=conversation_history[chat_id],
        )
        reply = response.content[0].text
        conversation_history[chat_id].append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error("Error al llamar a Claude: %s", e)
        await update.message.reply_text("Error al procesar tu mensaje. Intentá de nuevo.")


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("notas", cmd_notas))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(handle_category_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot iniciado.")
    app.run_polling()


if __name__ == "__main__":
    main()
