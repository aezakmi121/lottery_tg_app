import os
import requests
import random
import asyncio
import re
import logging
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import ReplyKeyboardMarkup
from apscheduler.util import convert_to_datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime, timezone, timedelta

# Set up API tokens
CRYPTBOT_API_TOKEN = os.getenv('CRYPTBOT_API_TOKEN')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Set up logging
logging.basicConfig(level=logging.INFO)

# CryptoBot API endpoint
CRYPTBOT_API_URL = 'https://pay.crypt.bot/api/'

# Store participants and pool amounts for each tier
bronze_pool_participants = []
silver_pool_participants = []
gold_pool_participants = []

bronze_pool_amount = 0
silver_pool_amount = 0
gold_pool_amount = 0

# Pool entry fees for each tier
bronze_entry_fee = 10.0  # $10
silver_entry_fee = 25.0  # $25
gold_entry_fee = 50.0  # $50

# Track invoice statuses (In real-world, use a database)
invoice_tracker = {}

# Dictionary to store user wallet addresses
user_wallets = {}

# Add a set to store user chat IDs
user_chat_ids = set()

# Variables to store pool start and end times
next_bronze_start_time = None
next_bronze_end_time = None

next_silver_start_time = None
next_silver_end_time = None

next_gold_start_time = None
next_gold_end_time = None


# Bot's cut percentage
bot_cut_percentage = 10  # 10% cut

# Store the pool start time
pool_start_time = None  # To be set when the pool starts

# Add pool status tracking variables
bronze_pool_open = False
silver_pool_open = False
gold_pool_open = False


# Function to create an invoice for payment (only accepts USDT)
def create_invoice(amount, description, max_retries=3):
    url = CRYPTBOT_API_URL + 'createInvoice'
    headers = {
        'Content-Type': 'application/json',
        'Crypto-Pay-API-Token': CRYPTBOT_API_TOKEN
    }
    payload = {
        'amount': str(amount),
        'currency_type': 'fiat',
        'fiat': 'USD',
        'accepted_assets': 'USDT',
        'description': description,
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()

            if response.status_code == 200 and response.json().get('ok'):
                invoice_url = response.json()['result']['bot_invoice_url']
                invoice_id = response.json()['result']['invoice_id']
                invoice_tracker[invoice_id] = {'status': 'pending', 'amount': amount}
                return invoice_url, invoice_id
            else:
                logging.error(f"Error in invoice creation: {response.json()}")
                return None, None

        except requests.exceptions.RequestException as e:
            logging.error(f"HTTP request failed: {e}. Attempt {attempt + 1} of {max_retries}")
            # Implement exponential backoff
            time.sleep(2 ** attempt)

    # If all retries fail, log and return None
    logging.error("Failed to create invoice after multiple attempts.")
    return None, None

# Function to check payment status
def check_payment(invoice_id):
    url = CRYPTBOT_API_URL + 'getInvoice'
    headers = {
        'Content-Type': 'application/json',
        'Crypto-Pay-API-Token': CRYPTBOT_API_TOKEN
    }
    payload = {'invoice_id': invoice_id}
    response = requests.post(url, json=payload, headers=headers)

    if response.status_code == 200 and response.json().get('ok'):
        status = response.json()['result']['status']
        return status == 'paid'  # Return True if payment is confirmed
    return False

async def check_payment_status(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data['chat_id']
    invoice_id = job_data['invoice_id']
    pool_name = job_data['pool_name']
    pool_participants = job_data['pool_participants']

    # Check if the invoice is already confirmed or expired
    if invoice_tracker.get(invoice_id, {}).get('status') in ['paid', 'expired']:
        return

    if check_payment(invoice_id):
        # Payment is successful, mark invoice as 'paid'
        invoice_tracker[invoice_id]['status'] = 'paid'
        pool_participants.append({'chat_id': chat_id, 'invoice_id': invoice_id})

        # Update pool amount based on the pool name
        global bronze_pool_amount, silver_pool_amount, gold_pool_amount
        if pool_name == "Bronze Pool":
            bronze_pool_amount += bronze_entry_fee
        elif pool_name == "Silver Pool":
            silver_pool_amount += silver_entry_fee
        elif pool_name == "Gold Pool":
            gold_pool_amount += gold_entry_fee

        await context.bot.send_message(chat_id=chat_id, text=f"You have successfully joined the {pool_name}!")
    else:
        # Timeout after 15 minutes (900 seconds)
        if (datetime.now(timezone.utc) - job_data['creation_time']).total_seconds() > 900:
            invoice_tracker[invoice_id]['status'] = 'expired'
            await context.bot.send_message(chat_id=chat_id, text="Your payment verification has timed out. Please try again.")
        else:
            # Reschedule the payment check if not yet completed
            context.job_queue.run_once(check_payment_status, 60, data=job_data)


# Function to set the user's wallet address
async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Extract the wallet address from the user's message
    if context.args:
        wallet_address = context.args[0]

        # Basic validation of wallet address using a simple regex (expand this for specific formats)
        if not re.match(r'^[A-Za-z0-9]{5,}$', wallet_address):
            await context.bot.send_message(chat_id=chat_id, text="Invalid wallet address. Please try again.")
            return

        # Store the wallet address in the user_wallets dictionary
        user_wallets[chat_id] = wallet_address
        await context.bot.send_message(chat_id=chat_id, text=f"Your wallet address has been set to: {wallet_address}")
    else:
        await context.bot.send_message(chat_id=chat_id,
                                       text="Please provide a wallet address. Usage: /set_wallet <WALLET_ADDRESS>")

def transfer_to_winner(user_id, amount, asset='USDT', max_retries=3):
    if user_id not in user_wallets:
        logging.error(f"User ID {user_id} has not set a wallet address.")
        return False, "No wallet address found. Please set your wallet address using /set_wallet."

    wallet_address = user_wallets[user_id]
    prize_amount = amount * (1 - bot_cut_percentage / 100)
    url = CRYPTBOT_API_URL + 'transfer'
    headers = {
        'Content-Type': 'application/json',
        'Crypto-Pay-API-Token': CRYPTBOT_API_TOKEN
    }
    payload = {
        'user_id': wallet_address,
        'asset': asset,
        'amount': str(prize_amount),
        'spend_id': str(random.randint(1, 1000000)),
        'comment': 'Congratulations! You have won the lucky draw!'
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()

            if response.status_code == 200 and response.json().get('ok'):
                logging.info(f"Successfully transferred {prize_amount} {asset} to wallet address {wallet_address}.")
                return True, None
            else:
                error_message = response.json().get('error', {}).get('message', 'Unknown error')
                logging.error(f"Error during transfer: {error_message}")
                return False, error_message

        except requests.exceptions.RequestException as e:
            logging.error(f"HTTP request failed during transfer: {e}. Attempt {attempt + 1} of {max_retries}")
            time.sleep(2 ** attempt)  # Implement exponential backoff

    logging.error("Failed to transfer to winner after multiple attempts.")
    return False, "Transfer failed. Please try again later."


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_chat_ids.add(chat_id)  # Add the user to the set

    # Define the custom keyboard layout using emojis
    keyboard = [
        ["📜 Rules", "📊 Status"],
        ["🥉 Join Bronze", "🥈 Join Silver", "🥇 Join Gold"],
        ["👥 Players", "ℹ️ My Info"],
        ["💰 Pool Size", "🆘 Help"],
        ["👛 Set Wallet"]
    ]

    # Create a ReplyKeyboardMarkup object
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    # Send a message with the custom keyboard
    welcome_message = (
        "🎉 Welcome to the Lucky Draw Pool Bot! 🎉\n\n"
        "Use the buttons below to navigate through the commands.\n"
        "You can join pools, check pool status, view rules, and more!"
    )
    await context.bot.send_message(chat_id=chat_id, text=welcome_message, reply_markup=reply_markup)

# Function to broadcast a message to all users
async def broadcast_message(application, message):
    for chat_id in user_chat_ids:
        try:
            await application.bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logging.error(f"Error sending message to {chat_id}: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text  # Get the text from the pressed button

    # Map button texts to their respective commands
    if text == "📜 Rules":
        await rules(update, context)
    elif text == "📊 Status":
        await status(update, context)
    elif text == "🥉 Join Bronze":
        await handle_join(update, context, bronze_entry_fee, bronze_pool_participants, "Bronze Pool")
    elif text == "🥈 Join Silver":
        await handle_join(update, context, silver_entry_fee, silver_pool_participants, "Silver Pool")
    elif text == "🥇 Join Gold":
        await handle_join(update, context, gold_entry_fee, gold_pool_participants, "Gold Pool")
    elif text == "👥 Players":
        await players(update, context)
    elif text == "ℹ️ My Info":
        await my_info(update, context)
    elif text == "💰 Pool Size":
        await pool_size(update, context)
    elif text == "🆘 Help":
        await help_command(update, context)
    elif text == "👛 Set Wallet":
        await context.bot.send_message(chat_id=chat_id, text="Please use the /set_wallet command to set your wallet address.")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Unknown command. Please use the available buttons.")



# Command to display the rules
async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text=(
        "Welcome to the Lucky Draw Pool!\n"
        "1. The Bronze Pool opens every 24 hours and runs for a full day. Entry fee: $10. Use /join_bronze to participate.\n"
        "2. The Silver Pool opens every 3 days and runs for 24 hours. Entry fee: $25. Use /join_silver to participate.\n"
        "3. The Gold Pool opens every Sunday and runs for 24 hours. Entry fee: $50. Use /join_gold to participate.\n"
        "4. At the end of each pool's duration, a winner will be randomly selected.\n"
        "5. The prize is transferred to the winner after a 10% bot cut.\n"
        "6. Payments are handled via CryptoBot, and only USDT is accepted for all transactions.\n"
        "7. Make sure to join only one pool per cycle. Once you join, you cannot join the same pool until it resets."
    ))

# Command to show the number of players in each pool
async def players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bronze_count = len(bronze_pool_participants)
    silver_count = len(silver_pool_participants)
    gold_count = len(gold_pool_participants)
    await context.bot.send_message(chat_id=chat_id, text=(
        f"Current Players:\n"
        f"Bronze Pool: {bronze_count} players\n"
        f"Silver Pool: {silver_count} players\n"
        f"Gold Pool: {gold_count} players"
    ))

# Command to display user info and their pool status
async def my_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_pools = []
    for participant in bronze_pool_participants:
        if participant['chat_id'] == chat_id:
            user_pools.append(f"Bronze Pool (Invoice ID: {participant['invoice_id']})")
    for participant in silver_pool_participants:
        if participant['chat_id'] == chat_id:
            user_pools.append(f"Silver Pool (Invoice ID: {participant['invoice_id']})")
    for participant in gold_pool_participants:
        if participant['chat_id'] == chat_id:
            user_pools.append(f"Gold Pool (Invoice ID: {participant['invoice_id']})")

    if user_pools:
        await context.bot.send_message(chat_id=chat_id, text=f"Your Info:\n" + "\n".join(user_pools))
    else:
        await context.bot.send_message(chat_id=chat_id, text="You are not currently in any pool.")

# Command to display the current pool size
async def pool_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text=(
        f"Current Pool Sizes:\n"
        f"Bronze Pool: ${bronze_pool_amount:.2f}\n"
        f"Silver Pool: ${silver_pool_amount:.2f}\n"
        f"Gold Pool: ${gold_pool_amount:.2f}"
    ))

# Handle joining the pool
async def handle_join(update, context, entry_fee, pool_participants, pool_name):
    chat_id = update.effective_chat.id

    # Check if user is already in the pool
    if any(participant['chat_id'] == chat_id for participant in pool_participants):
        await context.bot.send_message(chat_id=chat_id, text=f"You are already in the {pool_name}.")
        return

    payment_url, invoice_id = create_invoice(entry_fee, f"{pool_name} Entry")

    if payment_url:
        # Notify user to make the payment
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"To join the {pool_name}, please pay ${entry_fee} using this link: {payment_url}\n\n⏳ You have 15 minutes to complete the payment. If you fail to pay in time, you'll need to try again.")


        # Schedule the payment check using 'data'
        context.job_queue.run_once(check_payment_status, 60, data={
            'chat_id': chat_id,
            'invoice_id': invoice_id,
            'pool_name': pool_name,
            'pool_participants': pool_participants,
            'creation_time': datetime.now(timezone.utc)
        })
    else:
        await context.bot.send_message(chat_id=chat_id, text="Error creating payment. Please try again later.")


# Functions to start and end pools
# Modify start functions to set next opening and closing times
async def start_bronze_pool(context):
    global bronze_pool_open, next_bronze_end_time
    bronze_pool_open = True
    next_bronze_end_time = datetime.now(timezone.utc) + timedelta(hours=24)  # Pool runs for 24 hours

    # Notify all users
    message = "🥉 The Bronze Pool is now open and will close in 24 hours! Use /join_bronze to participate."
    await broadcast_message(context.application, message)

    await context.bot.send_message(chat_id=context.job.context, text="The Bronze Pool is now open! Use /join_bronze to participate.")

async def end_bronze_pool(context):
    global bronze_pool_open, next_bronze_start_time
    bronze_pool_open = False
    next_bronze_start_time = datetime.now(timezone.utc) + timedelta(days=1)  # Opens next day at 0:00
    await end_specific_pool(context, bronze_pool_participants, bronze_pool_amount, "Bronze Pool")

async def start_silver_pool(context):
    global silver_pool_open, next_silver_end_time
    silver_pool_open = True
    next_silver_end_time = datetime.now(timezone.utc) + timedelta(hours=24)  # Pool runs for 24 hours

    # Notify all users
    message = "🥈 The Silver Pool is now open and will close in 24 hours! Use /join_silver to participate."
    await broadcast_message(context.application, message)

    await context.bot.send_message(chat_id=context.job.context, text="The Silver Pool is now open! Use /join_silver to participate.")

async def end_silver_pool(context):
    global silver_pool_open, next_silver_start_time
    silver_pool_open = False
    next_silver_start_time = datetime.now(timezone.utc) + timedelta(days=3)  # Opens every 3 days
    await end_specific_pool(context, silver_pool_participants, silver_pool_amount, "Silver Pool")

async def start_gold_pool(context):
    global gold_pool_open, next_gold_end_time
    gold_pool_open = True
    next_gold_end_time = datetime.now(timezone.utc) + timedelta(hours=24)  # Pool runs for 24 hours

    # Notify all users
    message = "🥇 The Gold Pool is now open and will close in 24 hours! Use /join_gold to participate."
    await broadcast_message(context.application, message)

    await context.bot.send_message(chat_id=context.job.context, text="The Gold Pool is now open! Use /join_gold to participate.")

async def end_gold_pool(context):
    global gold_pool_open, next_gold_start_time
    gold_pool_open = False
    next_gold_start_time = datetime.now(timezone.utc) + timedelta(days=7)  # Opens every Sunday
    await end_specific_pool(context, gold_pool_participants, gold_pool_amount, "Gold Pool")

# Helper function to format the time remaining
def format_time_remaining(time_remaining):
    days, seconds = time_remaining.days, time_remaining.seconds
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"

# Implement the /status command
# Updated /status command
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Determine the status and time information for each pool
    bronze_status = "Open" if bronze_pool_open else "Closed"
    if bronze_pool_open:
        time_left_bronze = format_time_remaining(next_bronze_end_time - datetime.now(timezone.utc))
        bronze_info = f"Closes in: {time_left_bronze}"
    else:
        if next_bronze_start_time:
            time_until_bronze_open = format_time_remaining(next_bronze_start_time - datetime.now(timezone.utc))
            bronze_info = f"Opens in: {time_until_bronze_open}"
        else:
            bronze_info = "N/A"

    silver_status = "Open" if silver_pool_open else "Closed"
    if silver_pool_open:
        time_left_silver = format_time_remaining(next_silver_end_time - datetime.now(timezone.utc))
        silver_info = f"Closes in: {time_left_silver}"
    else:
        if next_silver_start_time:
            time_until_silver_open = format_time_remaining(next_silver_start_time - datetime.now(timezone.utc))
            silver_info = f"Opens in: {time_until_silver_open}"
        else:
            silver_info = "N/A"

    gold_status = "Open" if gold_pool_open else "Closed"
    if gold_pool_open:
        time_left_gold = format_time_remaining(next_gold_end_time - datetime.now(timezone.utc))
        gold_info = f"Closes in: {time_left_gold}"
    else:
        if next_gold_start_time:
            time_until_gold_open = format_time_remaining(next_gold_start_time - datetime.now(timezone.utc))
            gold_info = f"Opens in: {time_until_gold_open}"
        else:
            gold_info = "N/A"

    # Create a message showing the pool status
    status_message = (
        f"🟢 **Pool Status** 🟢\n"
        f"Bronze Pool: {bronze_status}, Current Size: ${bronze_pool_amount:.2f}\n    {bronze_info}\n"
        f"Silver Pool: {silver_status}, Current Size: ${silver_pool_amount:.2f}\n    {silver_info}\n"
        f"Gold Pool: {gold_status}, Current Size: ${gold_pool_amount:.2f}\n    {gold_info}\n"
    )

    # Send the status message to the user
    await context.bot.send_message(chat_id=chat_id, text=status_message, parse_mode='Markdown')

async def end_specific_pool(context, pool_participants, pool_amount, pool_name):
    # Function to select a winner and reset the pool
    def select_winner(pool_participants, pool_amount):
        if pool_participants:
            winner = random.choice(pool_participants)
            winner_chat_id = winner['chat_id']
            prize_amount = pool_amount * (1 - bot_cut_percentage / 100)
            success, error_message = transfer_to_winner(winner_chat_id, prize_amount)
            if success:
                return winner_chat_id, prize_amount
            else:
                return None, error_message
        return None, 0

    winner_chat_id, prize_amount = select_winner(pool_participants, pool_amount)
    if winner_chat_id:
        await context.bot.send_message(chat_id=winner_chat_id, text=f"Congratulations! You won ${prize_amount:.2f} in the {pool_name}!")
    else:
        await context.bot.send_message(chat_id=context.job.context, text=f"No winner selected for {pool_name} due to an error.")

    # Notify users of pool reset
    for participant in pool_participants:
        await context.bot.send_message(chat_id=participant['chat_id'], text="The pool has been reset for the next round. Join again to participate!")

    # Reset pool
    pool_participants.clear()
    if pool_name == "Bronze Pool":
        global bronze_pool_amount
        bronze_pool_amount = 0
    elif pool_name == "Silver Pool":
        global silver_pool_amount
        silver_pool_amount = 0
    elif pool_name == "Gold Pool":
        global gold_pool_amount
        gold_pool_amount = 0

# Command to show all available commands
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    help_text = (
        "Here are the available commands:\n"
        "/start - Start interacting with the bot and see basic instructions.\n"
        "/rules - Learn how the lucky draw pools work, including entry fees and pool timings.\n"
        "/join_bronze - Join the Bronze Pool ($10 entry fee).\n"
        "/join_silver - Join the Silver Pool ($25 entry fee).\n"
        "/join_gold - Join the Gold Pool ($50 entry fee).\n"
        "/players - View the number of participants in each pool.\n"
        "/my_info - See your participation status in the pools.\n"
        "/pool_size - View the current size of each pool in dollars.\n"
        "/status - Check the status of each pool, including whether they are open or closed, the current size, and the time left until they close or reopen.\n"
        "/help - Display this list of commands with their descriptions.\n"
        "/set_wallet - Set a wallet where the winning amount will be transferred"
    )
    await context.bot.send_message(chat_id=chat_id, text=help_text)

# Set up the bot
# Set up the bot
from telegram.ext import MessageHandler, filters

def main():
    global next_bronze_start_time, next_silver_start_time, next_gold_start_time

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Set up the scheduler
    scheduler = AsyncIOScheduler()

    # Schedule pool start and end times
    # Bronze Pool
    bronze_trigger = CronTrigger(hour=0, minute=0, timezone='Asia/Kolkata')
    scheduler.add_job(start_bronze_pool, bronze_trigger, args=[application])
    scheduler.add_job(end_bronze_pool, CronTrigger(hour=23, minute=59, timezone='Asia/Kolkata'), args=[application])
    next_bronze_start_time = bronze_trigger.get_next_fire_time(None, datetime.now(timezone.utc))

    # Silver Pool
    silver_trigger = CronTrigger(hour=0, minute=0, timezone='Asia/Kolkata', day='*/3')
    scheduler.add_job(start_silver_pool, silver_trigger, args=[application])
    scheduler.add_job(end_silver_pool, CronTrigger(hour=23, minute=59, timezone='Asia/Kolkata', day='*/3'), args=[application])
    next_silver_start_time = silver_trigger.get_next_fire_time(None, datetime.now(timezone.utc))

    # Gold Pool
    gold_trigger = CronTrigger(day_of_week='sun', hour=0, minute=0, timezone='Asia/Kolkata')
    scheduler.add_job(start_gold_pool, gold_trigger, args=[application])
    scheduler.add_job(end_gold_pool, CronTrigger(day_of_week='sun', hour=23, minute=59, timezone='Asia/Kolkata'), args=[application])
    next_gold_start_time = gold_trigger.get_next_fire_time(None, datetime.now(timezone.utc))

    # Start the scheduler
    scheduler.start()

    # Command handlers
    application.add_handler(CommandHandler('start', start_command))
    application.add_handler(CommandHandler('join_bronze', lambda u, c: handle_join(u, c, bronze_entry_fee, bronze_pool_participants, "Bronze Pool")))
    application.add_handler(CommandHandler('join_silver', lambda u, c: handle_join(u, c, silver_entry_fee, silver_pool_participants, "Silver Pool")))
    application.add_handler(CommandHandler('join_gold', lambda u, c: handle_join(u, c, gold_entry_fee, gold_pool_participants, "Gold Pool")))
    application.add_handler(CommandHandler('set_wallet', set_wallet))

    # Other command handlers
    application.add_handler(CommandHandler('rules', rules))
    application.add_handler(CommandHandler('players', players))
    application.add_handler(CommandHandler('my_info', my_info))
    application.add_handler(CommandHandler('pool_size', pool_size))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('status', status))

    # Add a message handler for the custom keyboard buttons
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))

    print("Starting Lucky Draw Pool bot...")
    application.run_polling()

if __name__ == '__main__':
    main()
