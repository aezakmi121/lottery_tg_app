import os
import requests
import random
import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
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

# Bot's cut percentage
bot_cut_percentage = 10  # 10% cut

# Store the pool start time
pool_start_time = None  # To be set when the pool starts

# Add pool status tracking variables
bronze_pool_open = False
silver_pool_open = False
gold_pool_open = False


# Function to create an invoice for payment (only accepts USDT)
def create_invoice(amount, description):
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
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        if response.status_code == 200 and response.json().get('ok'):
            invoice_url = response.json()['result']['bot_invoice_url']
            invoice_id = response.json()['result']['invoice_id']
            # Track invoice status
            invoice_tracker[invoice_id] = {'status': 'pending', 'amount': amount}
            return invoice_url, invoice_id
        else:
            logging.error(f"Error in invoice creation: {response.json()}")
    except requests.RequestException as e:
        logging.error(f"HTTP request failed: {e}")
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

# Function to transfer the prize to the winner
def transfer_to_winner(user_id, amount, asset='USDT'):
    valid_user_ids = [participant['chat_id'] for participant in
                      bronze_pool_participants + silver_pool_participants + gold_pool_participants]

    if user_id not in valid_user_ids:
        logging.error(f"Invalid user ID {user_id} for transfer. This user is not a participant in the current pools.")
        return False, "Invalid user ID for transfer."

    # Calculate the prize amount after deducting the bot's cut
    prize_amount = amount * (1 - bot_cut_percentage / 100)

    url = CRYPTBOT_API_URL + 'transfer'
    headers = {
        'Content-Type': 'application/json',
        'Crypto-Pay-API-Token': CRYPTBOT_API_TOKEN
    }
    payload = {
        'user_id': user_id,
        'asset': asset,
        'amount': str(prize_amount),
        'spend_id': str(random.randint(1, 1000000)),  # Unique identifier for this transfer
        'comment': 'Congratulations! You have won the lucky draw!'
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()  # Raise an HTTPError if the HTTP request returned an unsuccessful status code

        # Check if the response indicates a successful transfer
        if response.status_code == 200 and response.json().get('ok'):
            logging.info(f"Successfully transferred {prize_amount} {asset} to user ID {user_id}.")
            return True, None
        else:
            error_message = response.json().get('error', {}).get('message', 'Unknown error')
            logging.error(f"Error during transfer: {error_message}")
            return False, error_message
    except requests.RequestException as e:
        logging.error(f"HTTP request failed during transfer: {e}")
        return False, str(e)

# Command to handle the /start command
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    welcome_message = (
        "Welcome to the Lucky Draw Pool Bot!\n\n"
        "You can participate in daily, 3-day, and weekly pools to win amazing prizes.\n"
        "Here are the available commands to get started:\n"
        "/rules - Learn how the lucky draw pools work.\n"
        "/join_bronze - Join the Bronze Pool ($10 entry fee).\n"
        "/join_silver - Join the Silver Pool ($25 entry fee).\n"
        "/join_gold - Join the Gold Pool ($50 entry fee).\n"
        "/players - View the number of participants in each pool.\n"
        "/my_info - See your participation status and pool information.\n"
        "/pool_size - View the current size of each pool.\n"
        "/time_left - Check how much time is left until the pool closes.\n"
        "/help - Display the list of all commands."
    )
    await context.bot.send_message(chat_id=chat_id, text=welcome_message)


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
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"To join the {pool_name}, please pay ${entry_fee} using this link: {payment_url}")

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
# Modify start and end functions to update pool status
async def start_bronze_pool(context):
    global pool_start_time, bronze_pool_open
    pool_start_time = datetime.now(timezone.utc)
    bronze_pool_open = True
    await context.bot.send_message(chat_id=context.job.context, text="The Bronze Pool is now open! Use /join_bronze to participate.")

async def end_bronze_pool(context):
    global bronze_pool_open
    bronze_pool_open = False
    await end_specific_pool(context, bronze_pool_participants, bronze_pool_amount, "Bronze Pool")

# Add similar modifications for silver and gold pools
async def start_silver_pool(context):
    global pool_start_time, silver_pool_open
    pool_start_time = datetime.now(timezone.utc)
    silver_pool_open = True
    await context.bot.send_message(chat_id=context.job.context, text="The Silver Pool is now open! Use /join_silver to participate.")

async def end_silver_pool(context):
    global silver_pool_open
    silver_pool_open = False
    await end_specific_pool(context, silver_pool_participants, silver_pool_amount, "Silver Pool")

async def start_gold_pool(context):
    global pool_start_time, gold_pool_open
    pool_start_time = datetime.now(timezone.utc)
    gold_pool_open = True
    await context.bot.send_message(chat_id=context.job.context, text="The Gold Pool is now open! Use /join_gold to participate.")

async def end_gold_pool(context):
    global gold_pool_open
    gold_pool_open = False
    await end_specific_pool(context, gold_pool_participants, gold_pool_amount, "Gold Pool")


# Implement the /status command
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Determine the status of each pool
    bronze_status = "Open" if bronze_pool_open else "Closed"
    silver_status = "Open" if silver_pool_open else "Closed"
    gold_status = "Open" if gold_pool_open else "Closed"

    # Create a message showing the pool status
    status_message = (
        f"🟢 **Pool Status** 🟢\n"
        f"Bronze Pool: {bronze_status}, Current Size: ${bronze_pool_amount:.2f}\n"
        f"Silver Pool: {silver_status}, Current Size: ${silver_pool_amount:.2f}\n"
        f"Gold Pool: {gold_status}, Current Size: ${gold_pool_amount:.2f}\n"
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
        "/start - Start interacting with the bot.\n"
        "/rules - Learn how the lucky draw pool works.\n"
        "/join_bronze - Join the Bronze Pool ($5 entry fee).\n"
        "/join_silver - Join the Silver Pool ($10 entry fee).\n"
        "/join_gold - Join the Gold Pool ($50 entry fee).\n"
        "/players - View the number of participants in each pool.\n"
        "/my_info - See your participation status and pool information.\n"
        "/pool_size - View the current size of each pool.\n"
        "/help - Display this list of commands."
    )
    await context.bot.send_message(chat_id=chat_id, text=help_text)

# Set up the bot
# Set up the bot
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Set up the scheduler
    scheduler = AsyncIOScheduler()

    # Schedule pool start and end times
    scheduler.add_job(start_bronze_pool, CronTrigger(hour=0, minute=0, timezone='Asia/Kolkata'), args=[application])
    scheduler.add_job(end_bronze_pool, CronTrigger(hour=23, minute=59, timezone='Asia/Kolkata'), args=[application])

    scheduler.add_job(start_silver_pool, CronTrigger(hour=0, minute=0, timezone='Asia/Kolkata', day='*/3'),
                      args=[application])
    scheduler.add_job(end_silver_pool, CronTrigger(hour=23, minute=59, timezone='Asia/Kolkata', day='*/3'),
                      args=[application])

    scheduler.add_job(start_gold_pool, CronTrigger(day_of_week='sun', hour=0, minute=0, timezone='Asia/Kolkata'),
                      args=[application])
    scheduler.add_job(end_gold_pool, CronTrigger(day_of_week='sun', hour=23, minute=59, timezone='Asia/Kolkata'),
                      args=[application])

    scheduler.start()

    # Add command handlers
    application.add_handler(CommandHandler('start', start_command))
    application.add_handler(CommandHandler('join_bronze',
                                           lambda u, c: handle_join(u, c, bronze_entry_fee, bronze_pool_participants,
                                                                    "Bronze Pool")))
    application.add_handler(CommandHandler('join_silver',
                                           lambda u, c: handle_join(u, c, silver_entry_fee, silver_pool_participants,
                                                                    "Silver Pool")))
    application.add_handler(CommandHandler('join_gold',
                                           lambda u, c: handle_join(u, c, gold_entry_fee, gold_pool_participants,
                                                                    "Gold Pool")))

    # Other command handlers
    application.add_handler(CommandHandler('rules', rules))
    application.add_handler(CommandHandler('players', players))
    application.add_handler(CommandHandler('my_info', my_info))
    application.add_handler(CommandHandler('pool_size', pool_size))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('status', status))  # Add the new /status command handler

    print("Starting Lucky Draw Pool bot...")
    application.run_polling()


if __name__ == '__main__':
    main()
