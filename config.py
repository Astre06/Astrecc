# config.py

import os

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = "8326116065:AAEl-k_PLGcqEhphvuTYYF7KwFa1URY6kvA" # Token from cc.py
TELEGRAM_CHAT_ID = "6679042143" # Chat ID from auth.py

# Stripe and API Configuration
API_URL = "https://www.nutritionaledge.co.uk/my-account/add-payment-method/"
STRIPE_URL = "https://api.stripe.com/v1/payment_methods"

# Processing Configuration
MAX_WORKERS = 5
RETRY_COUNT = 3
RETRY_DELAY = 1