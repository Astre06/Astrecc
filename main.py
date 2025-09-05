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
from telegram import __version__ as TG_VER

try:
    from telegram import __version_info__
except ImportError:
    __version_info__ = (0, 0, 0, 0, 0)

if __version_info__ < (20, 0, 0, 'alpha', 1):
    raise RuntimeError(f"This bot requires at least v20 of python-telegram-bot. "
                       f"Your current version is {TG_VER}")

from config import TELEGRAM_BOT_TOKEN, MAX_WORKERS, DEFAULT_API_URL
from auth_processor import (
    generate_uuids,
    prepare_headers,
    check_card_across_sites,
    send_telegram_message,
)

SITE_STORAGE_FILE = "sites.txt"

def save_sites(sites):
    # Overwrite sites file with new list of sites
    with open(SITE_STORAGE_FILE, "w", encoding='utf-8') as f:
        for site in sites:
            f.write(site.strip() + "\n")

def load_sites():
    try:
        with open(SITE_STORAGE_FILE, "r", encoding='utf-8') as f:
            sites = [line.strip() for line in f if line.strip()]
            return sites if sites else [DEFAULT_API_URL]
    except FileNotFoundError:
        return [DEFAULT_API_URL]

def bin_lookup(card_number: str):
    import requests

    bin_number = card_number[:6]
    headers = {"Accept-Version": "3", "User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(f"https://lookup.binlist.net/{bin_number}", headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            scheme = str(data.get("scheme", "N/A")).upper()
            card_type = str(data.get("type", "N/A")).upper()
            level = str(data.get("brand", "STANDARD")).upper()
            bank = data.get("bank", {}).get("name", "Unknown Bank")
            country = data.get("country", {}).get("name", "Unknown Country")
            return f"{bin_number} - {level} - {card_type} - {scheme}", bank, country
        else:
            return f"{bin_number} - NOT FOUND", "Unknown Bank", "Unknown Country"
    except Exception:
        return f"{bin_number} - ERROR", "Unknown Bank", "Unknown Country"

def build_status_keyboard(card, total, processed, status, charged, cvv, ccn, low, declined):
    keyboard = [
        [InlineKeyboardButton(f"• {escape_markdown(card, version=2)} •", callback_data="noop")],
        [InlineKeyboardButton(f"• STATUS ➔ {escape_markdown(status, version=2)} •", callback_data="noop")],
        [InlineKeyboardButton(f"• CHARGED ➔ [ {charged} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• CVV ➔ [ {cvv} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• CCN ➔ [ {ccn} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• LOW FUNDS ➔ [ {low} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• DECLINED ➔ [ {declined} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• TOTAL ➔ [ {total} ] •", callback_data="noop")],
        [InlineKeyboardButton("✅ STOP", callback_data="stop")],
    ]
    return InlineKeyboardMarkup(keyboard)


from telegram.helpers import escape_markdown
msg = (
    "Send me a .txt file with one card per line in the format:\n"
    "`card|month|year|cvc`\n"
    "Example:\n"
    "`4242424242424242|12|2025|123`"
)
await update.message.reply_text(msg, parse_mode="MarkdownV2")


async def site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Replace Site(s)", callback_data="replace_site")]
    ]
    await update.message.reply_text("You want to replace the site(s)?", reply_markup=InlineKeyboardMarkup(keyboard))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "replace_site":
        await query.message.reply_text(
            "Please send the site information(s) in the format:\n"
            "SITE: https://example.com/my-account/add-payment-method/\n"
            "PAYMENT METHODS: [stripe]\n"
            "RESPONSE: Your Card Was Decline\n"
            "STATUS: The site can be used for API and in no-code checker.\n\n"
            "Or just send URLs, one per line."
        )
        context.user_data["awaiting_sites"] = True

    elif query.data == "stop":
        context.application.bot_data["stop"] = True
        await query.message.reply_text("Stop command acknowledged. Will stop after current card.")


async def capture_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_sites"):
        return

    sites = []
    # Extract all URLs from the message text, supports links in any line
    sites += re.findall(r"https?://[^\s]+", update.message.text)
    # Also handle SITE: prefix lines
    sites += re.findall(r"SITE:\s*(https?://[^\s]+)", update.message.text)

    sites = list(set(sites))  # remove duplicates

    if not sites:
        await update.message.reply_text("No valid site URLs found. Please send again.")
        return

    save_sites(sites)
    context.user_data["awaiting_sites"] = False
    await update.message.reply_text(f"Successfully saved {len(sites)} site(s) to be used for checking.")


async def chk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /chk <card_number> <month|year> <cvc>\nExample:\n"
            "/chk 4242424242424242 12|25 123"
        )
        return

    card_number = args[0]
    exp = args[1]
    cvc = args[2]

    if "|" not in exp:
        await update.message.reply_text("Expiry must be in MM|YY or MM|YYYY format.")
        return

    card_data = f"{card_number}|{exp}|{cvc}"

    sites = load_sites()

    headers = prepare_headers()
    uuids = generate_uuids()
    chat_id = update.effective_chat.id
    bot_token = context.bot.token

    loop = asyncio.get_running_loop()
    status, msg, raw = await loop.run_in_executor(
        None,
        check_card_across_sites,
        card_data,
        headers,
        uuids,
        chat_id,
        bot_token,
        sites,
    )

    await update.message.reply_text(f"{msg}")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Only .txt files are supported. Please upload a valid text file.")
        return

    file = await update.message.document.get_file()
    tmp_path = os.path.join(tempfile.gettempdir(), doc.file_name)
    await file.download_to_drive(tmp_path)

    with open(tmp_path) as f:
        cards = [line.strip() for line in f if line.strip() and len(line.split("|")) >= 3]

    if not cards:
        await update.message.reply_text("File empty or invalid format.")
        return

    preparing_msg = await update.message.reply_text("⚙️ Starting card checking...")
    await asyncio.sleep(1)
    await preparing_msg.delete()

    total = len(cards)
    charged, cvv, ccn, low, declined = 0, 0, 0, 0, 0
    collected_cards = []

    reply_msg = await update.message.reply_text(
        f"Checking 0/{total} cards. Please wait...",
        reply_markup=build_status_keyboard(
            "Waiting for first card",
            total,
            0,
            "Idle",
            charged,
            cvv,
            ccn,
            low,
            declined,
        ),
    )

    headers = prepare_headers()
    uuids = generate_uuids()
    chat_id = update.effective_chat.id
    bot_token = context.bot.token
    context.application.bot_data["stop"] = False

    async def run_checks():
        nonlocal charged, cvv, ccn, low, declined

        for idx, card in enumerate(cards, start=1):
            if context.application.bot_data["stop"]:
                await update.message.reply_text(f"🛑 Stopped at card {idx}.")
                break

            status, msg, raw = await asyncio.get_running_loop().run_in_executor(
                None,
                check_card_across_sites,
                card,
                headers,
                uuids,
                chat_id,
                bot_token,
                load_sites(),
            )

            if status == "CHARGED":
                charged += 1
            elif status == "CVV":
                cvv += 1
                collected_cards.append(f"{raw} | CVV")
            elif status == "CCN":
                ccn += 1
                collected_cards.append(f"{raw} | CCN")
            elif status == "LOW":
                low += 1
            else:
                declined += 1

            try:
                bin_info, bank, country = bin_lookup(raw.split("|")[0])
            except Exception:
                bin_info, bank, country = "Unknown", "Unknown", "Unknown"

            if status != "DECLINED":
                msg_to_send = (
                    f"Card: {raw}\n"
                    f"{msg}\n"
                    f"Bin Info: {bin_info}\n"
                    f"Bank: {bank}\n"
                    f"Country: {country}\n"
                )
                await update.message.reply_text(msg_to_send)

            try:
                await reply_msg.edit_text(
                    f"Checking {idx}/{total} cards...",
                    reply_markup=build_status_keyboard(
                        raw,
                        total,
                        idx,
                        status,
                        charged,
                        cvv,
                        ccn,
                        low,
                        declined,
                    ),
                )
            except Exception:
                pass

        await update.message.reply_text("✅ Finished all checks.")

        if collected_cards:
            filepath = os.path.join(tempfile.gettempdir(), "collected_cards.txt")
            with open(filepath, "w") as f:
                f.write("\n".join(collected_cards))
            await update.message.reply_document(
                InputFile(filepath),
                filename="collected_cards.txt",
                caption="Collected CVV & CCN cards",
            )

    asyncio.create_task(run_checks())


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Just to handle "noop" callbacks from status keyboard
    await update.callback_query.answer()


async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handler for the Stop button to stop processing gracefully
    context.application.bot_data["stop"] = True
    await update.callback_query.edit_message_text("🛑 Stopping after current card...")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("site", site))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("chk", chk))
    app.add_handler(CommandHandler("stop", stop_callback))  # Optional text command /stop
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), capture_sites))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern="^stop$"))

    print("🤖 Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()

