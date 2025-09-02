import asyncio
import tempfile
import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import TELEGRAM_BOT_TOKEN, MAX_WORKERS
from auth_processor import generate_uuids, prepare_headers, process_single_card

# BIN lookup function
def bin_lookup(card_number: str):
    bin_number = card_number[:6]
    headers = {"Accept-Version": "3", "User -Agent": "Mozilla/5.0"}
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

# Build inline keyboard for status display
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

# /start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a .txt file with one card per line in the format:\n"
        "`card|month|year|cvc`\n"
        "Example:\n"
        "`4242424242424242|12|2025|123`",
        parse_mode="Markdown"
    )

# Handle uploaded .txt file
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please upload a .txt file with card data.")
        return

    # Download file
    file = await doc.get_file()
    local_path = os.path.join(tempfile.gettempdir(), doc.file_name)
    await file.download_to_drive(local_path)

    # Read lines and clean
    with open(local_path, "r") as f:
        lines = [line.strip() for line in f if line.strip()]

    total = len(lines)
    if total == 0:
        await update.message.reply_text("❌ The file is empty.")
        return

    # Initial message
    preparing_msg = await update.message.reply_text("Preparing file ⚙️...")
    await asyncio.sleep(2)
    await preparing_msg.delete()

    # Counters
    charged_count = cvv_count = ccn_count = low_funds_count = declined_count = 0
    collected_cards = []

    reply_msg = await update.message.reply_text(
        f"Processing 0/{total}...",
        reply_markup=build_status_keyboard("Waiting for first card", total, 0, "Idle", charged_count, cvv_count, ccn_count, low_funds_count, declined_count)
    )

    uuids = generate_uuids()
    headers = prepare_headers()
    chat_id = update.message.chat_id
    bot_token = TELEGRAM_BOT_TOKEN

    loop = asyncio.get_running_loop()

    # Run card processing concurrently in thread pool
    tasks = [
        loop.run_in_executor(
            None,
            process_single_card,
            card,
            headers,
            uuids,
            chat_id,
            bot_token
        )
        for card in lines
    ]

    processed_count = 0
    for future in asyncio.as_completed(tasks):
        status_category, telegram_message, raw_card = await future
        processed_count += 1

        # Update counters
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

        # Send detailed message for each card
        try:
            bin_info, bank, country = bin_lookup(raw_card.split('|')[0])
        except Exception:
            bin_info, bank, country = "N/A", "N/A", "N/A"

        msg = (
            f"<b>CARD:</b> <code>{raw_card}</code>\n"
            f"<b>Gateway:</b> Stripe Auth\n"
            f"<b>Response:</b> {status_text} {'✅' if status_category in ['CHARGED', 'CVV', 'LOW_FUNDS'] else '❌'}\n\n"
            f"<b>Bin Info:</b> {bin_info}\n"
            f"<b>Bank:</b> {bank}\n"
            f"<b>Country:</b> {country}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

        # Update status keyboard
        try:
            await reply_msg.edit_text(
                f"Processing {processed_count}/{total}...",
                reply_markup=build_status_keyboard(raw_card, total, processed_count, status_text, charged_count, cvv_count, ccn_count, low_funds_count, declined_count)
            )
        except Exception:
            # Ignore edit errors (e.g., message deleted or flood limits)
            pass

    await update.message.reply_text("✅ Finished processing all cards.")

    # Save CVV + CCN cards to file and send
    if collected_cards:
        result_file = os.path.join(tempfile.gettempdir(), "results.txt")
        with open(result_file, "w") as f:
            f.write("\n".join(collected_cards))

        await update.message.reply_document(
            InputFile(result_file, filename="results.txt"),
            caption="📂 Collected CVV + CCN cards"
        )

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()