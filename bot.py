import logging
import sqlite3
import re
import os
from datetime import datetime, timedelta
from collections import defaultdict
from telegram import Update, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# --- Configuration ---
# Get token from environment variable (Railway) or hardcode as fallback
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("⚠️ WARNING: Using hardcoded token. Set BOT_TOKEN environment variable for production.")

# Admin IDs - can also be set via environment variable
ADMIN_IDS = []
admin_ids_str = os.environ.get("ADMIN_IDS", "")
if admin_ids_str:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup ---
def init_db():
    """Initialize the SQLite database for persistent storage."""
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS warnings
                 (user_id INTEGER, group_id INTEGER, count INTEGER, last_warning TIMESTAMP,
                 PRIMARY KEY (user_id, group_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY, 
                  warn_limit INTEGER DEFAULT 3,
                  mute_duration INTEGER DEFAULT 300,
                  auto_delete_spam INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS approved_links
                 (group_id INTEGER, link TEXT, PRIMARY KEY (group_id, link))''')
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

init_db()

# --- Database Helper Functions ---
def get_warn_limit(group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT warn_limit FROM settings WHERE group_id=?", (group_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 3

def get_mute_duration(group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT mute_duration FROM settings WHERE group_id=?", (group_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 300

def get_auto_delete(group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT auto_delete_spam FROM settings WHERE group_id=?", (group_id,))
    row = c.fetchone()
    conn.close()
    return bool(row[0]) if row else True

def update_settings(group_id, warn_limit=None, mute_duration=None, auto_delete_spam=None):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO settings (group_id) VALUES (?)", (group_id,))
    if warn_limit is not None:
        c.execute("UPDATE settings SET warn_limit=? WHERE group_id=?", (warn_limit, group_id))
    if mute_duration is not None:
        c.execute("UPDATE settings SET mute_duration=? WHERE group_id=?", (mute_duration, group_id))
    if auto_delete_spam is not None:
        c.execute("UPDATE settings SET auto_delete_spam=? WHERE group_id=?", (1 if auto_delete_spam else 0, group_id))
    conn.commit()
    conn.close()

def get_warnings(user_id, group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT count FROM warnings WHERE user_id=? AND group_id=?", (user_id, group_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def add_warning(user_id, group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT INTO warnings (user_id, group_id, count, last_warning) VALUES (?, ?, 1, ?) "
              "ON CONFLICT(user_id, group_id) DO UPDATE SET count=count+1, last_warning=?",
              (user_id, group_id, now, now))
    conn.commit()
    conn.close()

def reset_warnings(user_id, group_id):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM warnings WHERE user_id=? AND group_id=?", (user_id, group_id))
    conn.commit()
    conn.close()

def is_approved_link(group_id, link):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM approved_links WHERE group_id=? AND link=?", (group_id, link))
    row = c.fetchone()
    conn.close()
    return row is not None

def add_approved_link(group_id, link):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO approved_links (group_id, link) VALUES (?, ?)", (group_id, link))
    conn.commit()
    conn.close()

def remove_approved_link(group_id, link):
    conn = sqlite3.connect('spam_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM approved_links WHERE group_id=? AND link=?", (group_id, link))
    conn.commit()
    conn.close()

# --- Core Anti-Spam Logic ---
SPAM_PATTERNS = [
    r'(https?://[^\s]+){3,}',
    r'\b(free|earn|cash|click|win|prize|offer|limited|discount|bonus|gift|giveaway)\b.*\b(now|today|click|here|link)\b',
    r'\b(bit\.ly|tinyurl|shorturl|goo\.gl|ow\.ly|is\.gd|buff\.ly|t\.co|cutt\.ly|rb\.gy)\b',
]

def is_spam(text):
    """Check if the message text matches any spam pattern."""
    if not text:
        return False
    text_lower = text.lower()
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False

async def handle_message(update: Update, context: CallbackContext):
    """Process each message for spam."""
    if not update.message or not update.message.from_user:
        return
    
    user = update.message.from_user
    chat = update.message.chat
    
    # Ignore bots and private chats
    if user.is_bot or chat.type not in ['group', 'supergroup']:
        return

    # Get message text
    message_text = update.message.text or update.message.caption or ""

    # Check admin status
    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status in ['administrator', 'creator']:
            return  # Don't moderate admins
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    # Check for spam
    if is_spam(message_text):
        warn_limit = get_warn_limit(chat.id)
        current_warnings = get_warnings(user.id, chat.id)

        # Delete spam message
        if get_auto_delete(chat.id):
            try:
                await update.message.delete()
            except Exception as e:
                logger.error(f"Could not delete message: {e}")

        # Add warning
        add_warning(user.id, chat.id)
        new_warnings = current_warnings + 1

        # Send warning
        warning_text = f"🚨 Spam detected! {user.mention_html()} has received warning {new_warnings}/{warn_limit}."
        sent_msg = await update.message.reply_html(warning_text)

        # Mute if exceeded limit
        if new_warnings >= warn_limit:
            mute_duration = get_mute_duration(chat.id)
            try:
                until_date = datetime.now() + timedelta(seconds=mute_duration)
                await context.bot.restrict_chat_member(
                    chat.id, user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await sent_msg.edit_text(
                    f"🚫 {user.mention_html()} has been muted for {mute_duration//60} minutes due to spam.",
                    parse_mode='HTML'
                )
                reset_warnings(user.id, chat.id)
            except Exception as e:
                logger.error(f"Could not mute user: {e}")
                await sent_msg.edit_text(
                    f"⚠️ Could not mute {user.mention_html()}. Check bot permissions.",
                    parse_mode='HTML'
                )

# --- Admin Commands ---
async def set_warn_limit(update: Update, context: CallbackContext):
    """Set the number of warnings before mute."""
    if not await is_admin(update, context):
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /setlimit <number> (e.g., /setlimit 5)")
        return
    
    limit = int(args[0])
    if limit < 1:
        await update.message.reply_text("Limit must be at least 1.")
        return
    
    update_settings(update.message.chat.id, warn_limit=limit)
    await update.message.reply_text(f"✅ Warning limit set to {limit}.")

async def set_mute_duration(update: Update, context: CallbackContext):
    """Set mute duration in seconds."""
    if not await is_admin(update, context):
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /setmute <seconds> (e.g., /setmute 600)")
        return
    
    duration = int(args[0])
    if duration < 30:
        await update.message.reply_text("Duration must be at least 30 seconds.")
        return
    
    update_settings(update.message.chat.id, mute_duration=duration)
    await update.message.reply_text(f"✅ Mute duration set to {duration} seconds ({duration//60} minutes).")

async def toggle_auto_delete(update: Update, context: CallbackContext):
    """Toggle automatic deletion of spam messages."""
    if not await is_admin(update, context):
        return
    
    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Usage: /autodelete on/off")
        return
    
    status = args[0].lower() == 'on'
    update_settings(update.message.chat.id, auto_delete_spam=status)
    await update.message.reply_text(f"✅ Auto-delete {'enabled' if status else 'disabled'}.")

async def approve_link(update: Update, context: CallbackContext):
    """Approve a domain/link so it won't be flagged as spam."""
    if not await is_admin(update, context):
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /approve domain.com")
        return
    
    link = args[0].lower()
    add_approved_link(update.message.chat.id, link)
    await update.message.reply_text(f"✅ '{link}' has been added to the approved list.")

async def remove_approved(update: Update, context: CallbackContext):
    """Remove an approved link."""
    if not await is_admin(update, context):
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /remove domain.com")
        return
    
    link = args[0].lower()
    remove_approved_link(update.message.chat.id, link)
    await update.message.reply_text(f"✅ '{link}' removed from approved list.")

async def status(update: Update, context: CallbackContext):
    """Show current settings for the group."""
    if not update.message or not update.message.from_user:
        return
    
    chat = update.message.chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use this command in a group.")
        return

    warn_limit = get_warn_limit(chat.id)
    mute_duration = get_mute_duration(chat.id)
    auto_delete = get_auto_delete(chat.id)
    
    await update.message.reply_text(
        f"🛡️ **Anti-Spam Settings for this group**\n"
        f"• Warning Limit: {warn_limit}\n"
        f"• Mute Duration: {mute_duration} seconds ({mute_duration//60} min)\n"
        f"• Auto-Delete Spam: {'✅ Enabled' if auto_delete else '❌ Disabled'}\n"
        f"• Bot Status: 🟢 Active",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: CallbackContext):
    """Provide help text."""
    help_text = (
        "🤖 **Anti-Spam Bot Help**\n\n"
        "This bot detects and prevents spam in groups.\n\n"
        "**Admin Commands:**\n"
        "/setlimit <number> - Set warnings before mute\n"
        "/setmute <seconds> - Set mute duration\n"
        "/autodelete on/off - Toggle auto-deletion of spam\n"
        "/approve <domain> - Add domain to whitelist\n"
        "/remove <domain> - Remove domain from whitelist\n"
        "/status - Show current settings\n"
        "/help - Show this message\n\n"
        "**All commands (except /help) are admin-only.**"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def is_admin(update: Update, context: CallbackContext) -> bool:
    """Check if user is admin in the group."""
    if not update.message or not update.message.from_user:
        return False
    
    user = update.message.from_user
    chat = update.message.chat
    
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Use this command in a group.")
        return False
    
    # Check if user is in ADMIN_IDS
    if user.id in ADMIN_IDS:
        return True
    
    # Check Telegram admin status
    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
        if chat_member.status in ['administrator', 'creator']:
            return True
        else:
            await update.message.reply_text("⚠️ Only admins can use this command.")
            return False
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        await update.message.reply_text("❌ Error verifying admin status.")
        return False

# --- Error Handler ---
async def error_handler(update: object, context: CallbackContext):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

# --- Main Function ---
def main():
    """Start the bot."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ BOT_TOKEN not set! Please set the BOT_TOKEN environment variable.")
        return

    logger.info("🚀 Starting Anti-Spam Bot...")
    
    # Create Application with proper configuration
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Message handler for spam detection
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.CAPTION, handle_message))

    # Command handlers
    application.add_handler(CommandHandler("setlimit", set_warn_limit))
    application.add_handler(CommandHandler("setmute", set_mute_duration))
    application.add_handler(CommandHandler("autodelete", toggle_auto_delete))
    application.add_handler(CommandHandler("approve", approve_link))
    application.add_handler(CommandHandler("remove", remove_approved))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("help", help_command))

    # Error handler
    application.add_error_handler(error_handler)

    # Start the Bot with proper settings
    logger.info("✅ Bot is running and ready to moderate!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
