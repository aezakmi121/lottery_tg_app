import os
import requests
import random
import asyncio
import re
import logging
import time
import psycopg2
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import ReplyKeyboardMarkup
from datetime import datetime, timezone, timedelta

# Set up API tokens
CRYPTBOT_API_TOKEN = os.getenv('CRYPTBOT_API_TOKEN')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Set up the database connection
DATABASE_URL = os.getenv('DATABASE_URL')

def get_db_connection():
    try:
        logging.info("Attempting to connect to the database...")
        connection = psycopg2.connect(DATABASE_URL, sslmode='require')
        logging.info("Database connection established successfully.")
        return connection
    except Exception as e:
        logging.error(f"Failed to connect to the database: {e}")
        raise e  # Reraise the exception to let the calling function handle it

# Set up logging
logging.basicConfig(level=logging.INFO)

# CryptoBot API endpoint
CRYPTBOT_API_URL = 'https://pay.crypt.bot/api/'

# Pool entry fees
bronze_entry_fee = 10.0
silver_entry_fee = 25.0
gold_entry_fee = 50.0

# Pool status variables
next_bronze_start_time = None
next_bronze_end_time = None

next_silver_start_time = None
next_silver_end_time = None

next_gold_start_time = None
next_gold_end_time = None

# Bot's cut percentage
bot_cut_percentage = 10

# Function to create an invoice for payment (only accepts USDT)
def create_invoice(amount, description, chat_id, pool_name, max_retries=3):
    url = CRYPTBOT_API_URL + 'createInvoice'
    headers = {'Content-Type': 'application/json', 'Crypto-Pay-API-Token': CRYPTBOT_API_TOKEN}
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

                # Store the invoice in the database
                try:
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO invoices (invoice_id, chat_id, amount, status, pool_name, creation_time)
                        VALUES (%s, %s, %s, %s, %s, %s);
                    """, (invoice_id, chat_id, amount, 'pending', pool_name, datetime.now(timezone.utc)))
                    
                    conn.commit()
                    cur.close()
                    conn.close()
                except Exception as e:
                    logging.error(f"Database error: {e}")

                return invoice_url, invoice_id
            else:
                logging.error(f"Error in invoice creation: {response.json()}")
                return None, None

        except requests.exceptions.RequestException as e:
            logging.error(f"HTTP request failed: {e}. Attempt {attempt + 1} of {max_retries}")
            time.sleep(2 ** attempt)

    logging.error("Failed to create invoice after multiple attempts.")
    return None, None

# Function to check payment status
def check_payment(invoice_id):
    url = CRYPTBOT_API_URL + 'getInvoice'
    headers = {'Content-Type': 'application/json', 'Crypto-Pay-API-Token': CRYPTBOT_API_TOKEN}
    payload = {'invoice_id': invoice_id}
    response = requests.post(url, json=payload, headers=headers)

    if response.status_code == 200 and response.json().get('ok'):
        status = response.json()['result']['status']
        return status == 'paid'
    return False

# Updated check_payment_status without invoice_tracker usage
async def check_payment_status(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data['chat_id']
    invoice_id = job_data['invoice_id']
    pool_name = job_data['pool_name']

    if check_payment(invoice_id):
        # Payment is successful, mark invoice as 'paid'
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            # Update the invoice status to 'paid'
            cur.execute("UPDATE invoices SET status = 'paid' WHERE invoice_id = %s;", (invoice_id,))
            
            # Insert into pool_participants
            cur.execute("INSERT INTO pool_participants (chat_id, pool_name, invoice_id) VALUES (%s, %s, %s);", (chat_id, pool_name, invoice_id))
            
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Database error: {e}")

        # Send confirmation message
        await context.bot.send_message(chat_id=chat_id, text=f"You have successfully joined the {pool_name}!")
    else:
        # Timeout after 15 minutes (900 seconds)
        if (datetime.now(timezone.utc) - job_data['creation_time']).total_seconds() > 900:
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                # Update the invoice status to 'expired'
                cur.execute("UPDATE invoices SET status = 'expired' WHERE invoice_id = %s;", (invoice_id,))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logging.error(f"Database error while updating invoice status: {e}")

            await context.bot.send_message(chat_id=chat_id, text="Your payment verification has timed out. Please try again.")
        else:
            # Reschedule the payment check if not yet completed
            context.job_queue.run_once(check_payment_status, 60, data=job_data)       
# Function to set the user's wallet address
async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args:
        wallet_address = context.args[0]

        # Basic validation of wallet address using regex
        if not re.match(r'^[A-Za-z0-9]{5,}$', wallet_address):
            await context.bot.send_message(chat_id=chat_id, text="Invalid wallet address. Please try again.")
            return
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Insert or update the user's wallet address in the database
            cur.execute("""
                INSERT INTO users (chat_id, wallet_address) 
                VALUES (%s, %s)
                ON CONFLICT (chat_id) 
                DO UPDATE SET wallet_address = EXCLUDED.wallet_address;
            """, (chat_id, wallet_address))
            
            conn.commit()
            cur.close()
            conn.close()

            await context.bot.send_message(chat_id=chat_id, text=f"Your wallet address has been set to: {wallet_address}")
        except Exception as e:
            logging.error(f"Database error: {e}")
            await context.bot.send_message(chat_id=chat_id, text="An error occurred while setting your wallet. Please try again.")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Please provide a wallet address. Usage: /set_wallet <WALLET_ADDRESS>")

def transfer_to_winner(user_id, amount, asset='USDT', max_retries=3):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Fetch the wallet address from the database
        cur.execute("""
            SELECT wallet_address FROM users WHERE chat_id = %s;
        """, (user_id,))
        result = cur.fetchone()
        cur.close()
        conn.close()

        if result is None:
            logging.error(f"User ID {user_id} has not set a wallet address.")
            return False, "No wallet address found. Please set your wallet address using /set_wallet."

        wallet_address = result[0]
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
                    
                    # Log the successful transfer to the database
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO transfers (chat_id, amount, asset, status, timestamp)
                        VALUES (%s, %s, %s, %s, %s);
                    """, (user_id, prize_amount, asset, 'successful', datetime.now(timezone.utc)))
                    conn.commit()
                    cur.close()
                    conn.close()

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
    
    except Exception as e:
        logging.error(f"Database error: {e}")
        return False, "Database error while fetching wallet address."

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logging.info(f"Received /start command from chat_id: {chat_id}")
    
    try:
        # Insert the user into the database if they don't already exist
        logging.info(f"Attempting to insert user {chat_id} into the database.")
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (chat_id) 
            VALUES (%s)
            ON CONFLICT (chat_id) DO NOTHING;
        """, (chat_id,))
        conn.commit()
        cur.close()
        conn.close()
        logging.info(f"User {chat_id} inserted successfully.")
    
    except Exception as e:
        logging.error(f"Database error in start_command: {e}")
        await context.bot.send_message(chat_id=chat_id, text="An error occurred while registering you. Please try again.")
        return

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
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Fetch all chat_ids from the users table
        cur.execute("SELECT chat_id FROM users;")
        chat_ids = cur.fetchall()

        cur.close()
        conn.close()

        # Use async gather to handle multiple send_message calls concurrently
        await asyncio.gather(*[
            application.bot.send_message(chat_id=chat_id[0], text=message) for chat_id in chat_ids
        ])

    except Exception as e:
        logging.error(f"Database error in broadcast_message: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text  # Get the text from the pressed button
    logging.info(f"Button pressed: {text} by chat_id: {chat_id}")
    
    # Map button texts to their respective commands
    if text == "📜 Rules":
        await rules(update, context)
    elif text == "📊 Status":
        await status(update, context)
    elif text == "🥉 Join Bronze":
        await handle_join(update, context, bronze_entry_fee, "Bronze Pool")
    elif text == "🥈 Join Silver":
        await handle_join(update, context, silver_entry_fee, "Silver Pool")
    elif text == "🥇 Join Gold":
        await handle_join(update, context, gold_entry_fee, "Gold Pool")
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
        logging.warning(f"Unknown command received: {text}")
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

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Get counts for each pool
        cur.execute("SELECT COUNT(*) FROM pool_participants WHERE pool_name = %s;", ('Bronze Pool',))
        bronze_count = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM pool_participants WHERE pool_name = %s;", ('Silver Pool',))
        silver_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM pool_participants WHERE pool_name = %s;", ('Gold Pool',))
        gold_count = cur.fetchone()[0]

        cur.close()
        conn.close()

        await context.bot.send_message(chat_id=chat_id, text=(
            f"Current Players:\n"
            f"Bronze Pool: {bronze_count} players\n"
            f"Silver Pool: {silver_count} players\n"
            f"Gold Pool: {gold_count} players"
        ))
    
    except Exception as e:
        logging.error(f"Database error: {e}")
        await context.bot.send_message(chat_id=chat_id, text="An error occurred while fetching player counts. Please try again.")

# Command to display user info and their pool status
async def my_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    try:
        conn = get_db_connection()
        if conn is None:
            logging.error(f"Database connection failed for chat_id: {chat_id}")
            await context.bot.send_message(chat_id=chat_id, text="Database connection failed. Please try again later.")
            return

        cur = conn.cursor()
        logging.info(f"Executing query to fetch pool participation for chat_id: {chat_id}")
        
        # Execute the query
        cur.execute("SELECT pool_name, invoice_id FROM pool_participants WHERE chat_id = %s;", (chat_id,))
        
        # Fetch results
        user_pools = cur.fetchall()
        logging.info(f"Query result for chat_id {chat_id}: {user_pools}")

        cur.close()
        conn.close()

        if user_pools:
            pool_info = "\n".join([f"{pool_name} (Invoice ID: {invoice_id})" for pool_name, invoice_id in user_pools])
            await context.bot.send_message(chat_id=chat_id, text=f"Your Info:\n{pool_info}")
        else:
            await context.bot.send_message(chat_id=chat_id, text="You are not currently in any pool.")
    except Exception as e:
        logging.error(f"Database error in my_info for chat_id {chat_id}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="An error occurred while retrieving your info. Please try again.")

# Command to display the current pool size
async def pool_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Fetch pool sizes from the database
        cur.execute("SELECT pool_name, pool_amount FROM pools WHERE pool_name IN ('Bronze Pool', 'Silver Pool', 'Gold Pool');")
        pools = cur.fetchall()

        pool_sizes = {name: amount for name, amount in pools}
        bronze_amount = pool_sizes.get('Bronze Pool', 0)
        silver_amount = pool_sizes.get('Silver Pool', 0)
        gold_amount = pool_sizes.get('Gold Pool', 0)

        cur.close()
        conn.close()

        # Send pool sizes to the user
        await context.bot.send_message(chat_id=chat_id, text=(
            f"Current Pool Sizes:\n"
            f"Bronze Pool: ${bronze_amount:.2f}\n"
            f"Silver Pool: ${silver_amount:.2f}\n"
            f"Gold Pool: ${gold_amount:.2f}"
        ))
    except Exception as e:
        logging.error(f"Database error: {e}")
        await context.bot.send_message(chat_id=chat_id, text="An error occurred while fetching pool sizes. Please try again.")

# Handle joining the pool
async def handle_join(update, context, entry_fee, pool_name):
    chat_id = update.effective_chat.id
    logging.info(f"Handling join request for {pool_name} by chat_id: {chat_id}")
    
    try:
        # Connect to the database
        logging.info(f"Checking if user {chat_id} is already in the {pool_name}")
        conn = get_db_connection()
        cur = conn.cursor()

        # Check if user is already in the pool
        cur.execute("SELECT COUNT(*) FROM pool_participants WHERE chat_id = %s AND pool_name = %s;", (chat_id, pool_name))
        already_in_pool = cur.fetchone()[0] > 0

        cur.close()
        conn.close()

        # If the user is already in the pool, send a message and return
        if already_in_pool:
            logging.info(f"User {chat_id} is already in the {pool_name}")
            await context.bot.send_message(chat_id=chat_id, text=f"You are already in the {pool_name}.")
            return

    except Exception as e:
        logging.error(f"Database error while checking pool participation: {e}")
        await context.bot.send_message(chat_id=chat_id, text="An error occurred while checking pool participation. Please try again.")
        return

    # Continue with invoice creation
    logging.info(f"Creating invoice for user {chat_id} to join the {pool_name}")
    payment_url, invoice_id = create_invoice(entry_fee, f"{pool_name} Entry", chat_id, pool_name)

    # If invoice creation was successful, proceed
    if payment_url:
        try:
            logging.info(f"Invoice created successfully for user {chat_id}. Payment URL: {payment_url}")
            # Notify the user to make the payment
            await context.bot.send_message(chat_id=chat_id,
                                           text=f"To join the {pool_name}, please pay ${entry_fee} using this link: {payment_url}\n\n⏳ You have 15 minutes to complete the payment. If you fail to pay in time, you'll need to try again.")
            
            # Schedule the payment check
            context.job_queue.run_once(check_payment_status, 60, data={
                'chat_id': chat_id,
                'invoice_id': invoice_id,
                'pool_name': pool_name,
                'creation_time': datetime.now(timezone.utc)
            })
        
        except Exception as e:
            logging.error(f"Error while scheduling payment check: {e}")
            await context.bot.send_message(chat_id=chat_id, text="An error occurred while scheduling payment verification. Please try again.")
    else:
        # If invoice creation failed, notify the user
        logging.error(f"Failed to create an invoice for user {chat_id}")
        await context.bot.send_message(chat_id=chat_id, text="Error creating payment. Please try again later.")

# Modify start functions to set next opening and closing times
async def start_bronze_pool(context):
    global next_bronze_end_time  # Removed bronze_pool_open as it is unused
    next_bronze_end_time = datetime.now(timezone.utc) + timedelta(hours=24)  # Pool runs for 24 hours

    # Notify all users
    message = "🥉 The Bronze Pool is now open and will close in 24 hours! Use /join_bronze to participate."
    await broadcast_message(context.application, message)

    await context.bot.send_message(chat_id=context.job.context, text="The Bronze Pool is now open! Use /join_bronze to participate.")

async def end_bronze_pool(context):
    global next_bronze_start_time  # Removed bronze_pool_open as it is unused
    next_bronze_start_time = datetime.now(timezone.utc) + timedelta(days=1)  # Opens next day at 0:00
    await end_specific_pool(context, bronze_pool_participants, bronze_pool_amount, "Bronze Pool")

async def start_silver_pool(context):
    global next_silver_end_time  # Removed silver_pool_open as it is unused
    next_silver_end_time = datetime.now(timezone.utc) + timedelta(hours=24)  # Pool runs for 24 hours

    # Notify all users
    message = "🥈 The Silver Pool is now open and will close in 24 hours! Use /join_silver to participate."
    await broadcast_message(context.application, message)

    await context.bot.send_message(chat_id=context.job.context, text="The Silver Pool is now open! Use /join_silver to participate.")

async def end_silver_pool(context):
    global next_silver_start_time  # Removed silver_pool_open as it is unused
    next_silver_start_time = datetime.now(timezone.utc) + timedelta(days=3)  # Opens every 3 days
    await end_specific_pool(context, silver_pool_participants, silver_pool_amount, "Silver Pool")

async def start_gold_pool(context):
    global next_gold_end_time  # Removed gold_pool_open as it is unused
    next_gold_end_time = datetime.now(timezone.utc) + timedelta(hours=24)  # Pool runs for 24 hours

    # Notify all users
    message = "🥇 The Gold Pool is now open and will close in 24 hours! Use /join_gold to participate."
    await broadcast_message(context.application, message)

    await context.bot.send_message(chat_id=context.job.context, text="The Gold Pool is now open! Use /join_gold to participate.")
    
async def end_gold_pool(context):
    global next_gold_start_time  # Removed gold_pool_open as it is unused
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

    try:
        # Fetch pool sizes from the database
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT pool_name, pool_amount FROM pools WHERE pool_name IN ('Bronze Pool', 'Silver Pool', 'Gold Pool');")
        pools = cur.fetchall()
        pool_sizes = {name: amount for name, amount in pools}

        # Assign amounts with a fallback value of 0 if not found
        bronze_pool_amount = pool_sizes.get('Bronze Pool', 0)
        silver_pool_amount = pool_sizes.get('Silver Pool', 0)
        gold_pool_amount = pool_sizes.get('Gold Pool', 0)

        cur.close()
        conn.close()

    except Exception as e:
        logging.error(f"Database error in /status command: {e}")
        await context.bot.send_message(chat_id=chat_id, text="An error occurred while fetching pool status. Please try again later.")
        return

    # Determine the status and time information for each pool
    now = datetime.now(timezone.utc)

    # Bronze Pool Status
    bronze_status = "Open" if next_bronze_start_time <= now < next_bronze_end_time else "Closed"
    if bronze_status == "Open":
        time_left_bronze = format_time_remaining(next_bronze_end_time - now)
        bronze_info = f"Closes in: {time_left_bronze}"
    else:
        if next_bronze_start_time:
            time_until_bronze_open = format_time_remaining(next_bronze_start_time - now)
            bronze_info = f"Opens in: {time_until_bronze_open}"
        else:
            bronze_info = "N/A"

    # Silver Pool Status
    silver_status = "Open" if next_silver_start_time <= now < next_silver_end_time else "Closed"
    if silver_status == "Open":
        time_left_silver = format_time_remaining(next_silver_end_time - now)
        silver_info = f"Closes in: {time_left_silver}"
    else:
        if next_silver_start_time:
            time_until_silver_open = format_time_remaining(next_silver_start_time - now)
            silver_info = f"Opens in: {time_until_silver_open}"
        else:
            silver_info = "N/A"

    # Gold Pool Status
    gold_status = "Open" if next_gold_start_time <= now < next_gold_end_time else "Closed"
    if gold_status == "Open":
        time_left_gold = format_time_remaining(next_gold_end_time - now)
        gold_info = f"Closes in: {time_left_gold}"
    else:
        if next_gold_start_time:
            time_until_gold_open = format_time_remaining(next_gold_start_time - now)
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

async def end_specific_pool(context, pool_participants, pool_name):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Fetch the current pool amount from the database
        cur.execute("SELECT pool_amount FROM pools WHERE pool_name = %s;", (pool_name,))
        pool_amount = cur.fetchone()[0]

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

        # Reset pool amount in the database
        cur.execute("UPDATE pools SET pool_amount = 0 WHERE pool_name = %s;", (pool_name,))
        conn.commit()

        cur.close()
        conn.close()

    except Exception as e:
        logging.error(f"Database error in end_specific_pool: {e}")

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

    logging.info("Setting up the bot application...")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Set up the scheduler
    scheduler = AsyncIOScheduler()
    
    logging.info("Setting up scheduled jobs for pools...")

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

    logging.info("Starting the scheduler...")
    scheduler.start()

    # Command handlers
    application.add_handler(CommandHandler('start', start_command))
    application.add_handler(CommandHandler('set_wallet', set_wallet))
    application.add_handler(CommandHandler('join_bronze', lambda u, c: handle_join(u, c, bronze_entry_fee, "Bronze Pool")))
    application.add_handler(CommandHandler('join_silver', lambda u, c: handle_join(u, c, silver_entry_fee, "Silver Pool")))
    application.add_handler(CommandHandler('join_gold', lambda u, c: handle_join(u, c, gold_entry_fee, "Gold Pool")))
    # Other command handlers
    application.add_handler(CommandHandler('rules', rules))
    application.add_handler(CommandHandler('players', players))
    application.add_handler(CommandHandler('my_info', my_info))
    application.add_handler(CommandHandler('pool_size', pool_size))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('status', status))

    # Add a message handler for the custom keyboard buttons
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))

    logging.info("Starting Lucky Draw Pool bot...")
    application.run_polling()

if __name__ == '__main__':
    main()
