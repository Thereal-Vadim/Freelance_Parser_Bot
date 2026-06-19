import os
import re
import html
import logging
import asyncio
import json
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
import db
import ml
from utils import parse_indices

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")

# Parse list of allowed users (IDs or usernames)
ALLOWED_IDS = set()
ALLOWED_USERNAMES = set()
for item in ALLOWED_USERS_RAW.split(","):
    item = item.strip()
    if not item:
        continue
    if item.isdigit():
        ALLOWED_IDS.add(int(item))
    else:
        # Remove @ symbol if present
        username = item.lstrip("@").lower()
        if username:
            ALLOWED_USERNAMES.add(username)

def is_user_allowed(user: types.User) -> bool:
    # If the allowed users lists are empty, access is open to everyone
    if not ALLOWED_IDS and not ALLOWED_USERNAMES:
        return True
    if user.id in ALLOWED_IDS:
        return True
    if user.username and user.username.lower() in ALLOWED_USERNAMES:
        return True
    return False

if not TG_TOKEN:
    logger.error("CRITICAL ERROR: TG_TOKEN environment variable is not set!")
if not SPREADSHEET_ID:
    logger.error("CRITICAL ERROR: SPREADSHEET_ID environment variable is not set!")

bot = Bot(token=TG_TOKEN) if TG_TOKEN else None
dp = Dispatcher()

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📋 Get Leads"),
                KeyboardButton(text="⚡ Get Leads (Force)")
            ],
            [
                KeyboardButton(text="🧠 Train ML Model"),
                KeyboardButton(text="❌ Cancel")
            ]
        ],
        resize_keyboard=True
    )
    return keyboard

class LeadStates(StatesGroup):
    reviewing = State()
    waiting_for_replace = State()

# Connection to Google Sheets (synchronous helper function)
_google_sheet_cache = None

def get_google_sheet():
    global _google_sheet_cache
    if _google_sheet_cache is None:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        if not os.path.exists(CREDENTIALS_FILE):
            raise FileNotFoundError(f"Credentials file '{CREDENTIALS_FILE}' not found. Please follow the instructions in README.md.")
        
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        workbook = client.open_by_key(SPREADSHEET_ID)
        # Use the last worksheet (current report)
        _google_sheet_cache = workbook.worksheets()[-1]
    return _google_sheet_cache

# Get OAuth2 access token for Google API
def get_drive_access_token():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(f"Credentials file '{CREDENTIALS_FILE}' not found.")
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    creds.refresh(Request())
    return creds.token

# Upload file to Google Drive and enable link sharing
def upload_file_to_drive(file_path, filename):
    try:
        access_token = get_drive_access_token()
        metadata = {
            "name": filename,
            "mimeType": "image/png"
        }
        files = {
            "data": ("metadata", json.dumps(metadata), "application/json"),
            "file": (filename, open(file_path, "rb"), "image/png")
        }
        upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
        res = requests.post(upload_url, headers={"Authorization": f"Bearer {access_token}"}, files=files)
        if res.status_code != 200:
            raise Exception(f"Upload failed: {res.text}")
        
        file_id = res.json().get("id")
        
        # Make file accessible via link
        permission = {
            "role": "reader",
            "type": "anyone"
        }
        perm_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions"
        requests.post(perm_url, headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }, json=permission)
        
        return file_id
    except Exception as e:
        logger.error(f"Error uploading to Google Drive: {e}")
        return None

# Extract page title from job board (synchronous function, runs in thread pool)
def fetch_vacancy_title(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.string if soup.title else "New Job"
            # Clean title of job board specific suffixes
            title = re.sub(r"\s*—\s*Кворк.*|\s*—\s*фриланс.*|\s*\|.*", "", title, flags=re.IGNORECASE)
            return title.strip()
        else:
            logger.warning(f"Failed to load page {url}, status code: {response.status_code}")
    except Exception as e:
        logger.error(f"Error parsing title for {url}: {e}")
    return "Job (failed to parse title)"

# Write row to Google Sheet (synchronous function, runs in thread pool)
def add_vacancy_to_sheet(title, vacancy_url, date_str, screenshot_url):
    sheet = get_google_sheet()
    all_rows = sheet.get_all_values()
    
    # Find the first empty row (no title and no URL) starting from the 10th row (index 9)
    target_row_idx = None
    for idx, row in enumerate(all_rows):
        if idx >= 9:
            val_title = row[1].strip() if len(row) > 1 else ""
            val_url = row[2].strip() if len(row) > 2 else ""
            if not val_title and not val_url:
                target_row_idx = idx + 1  # 1-based index in gspread
                break

    if target_row_idx is not None:
        # If the row already exists in the template (e.g. with a prefilled number in column A)
        existing_num = all_rows[target_row_idx - 1][0].strip() if len(all_rows[target_row_idx - 1]) > 0 else ""
        if existing_num.isdigit():
            row_num = int(existing_num)
        else:
            # Calculate row number based on the previous row
            row_num = 1
            for r in reversed(all_rows[:target_row_idx - 1]):
                if r and len(r) > 0 and str(r[0]).isdigit():
                    row_num = int(r[0]) + 1
                    break
                    
        new_row = [
            row_num, 
            title, 
            vacancy_url, 
            date_str, 
            "Applied", 
            "", 
            screenshot_url
        ]
        
        # Update existing row
        sheet.update(range_name=f"A{target_row_idx}:G{target_row_idx}", values=[new_row])
    else:
        # If no empty prefilled rows, calculate number and append new row at the end
        row_num = 1
        for r in reversed(all_rows):
            if r and len(r) > 0 and str(r[0]).isdigit():
                row_num = int(r[0]) + 1
                break
                
        new_row = [
            row_num, 
            title, 
            vacancy_url, 
            date_str, 
            "Applied", 
            "", 
            screenshot_url
        ]
        
        next_index = len(all_rows) + 1
        sheet.insert_row(new_row, next_index)
        
    return row_num

# Batch write rows to Google Sheet (for approved batch)
def add_vacancies_to_sheet(jobs_list):
    sheet = get_google_sheet()
    all_rows = sheet.get_all_values()
    
    # Find the first empty row index under numbers (after 10th row)
    target_row_idx = len(all_rows) + 1
    for idx, row in enumerate(all_rows):
        if idx >= 9: # Row 10 (0-indexed)
            val_title = row[1].strip() if len(row) > 1 else ""
            val_url = row[2].strip() if len(row) > 2 else ""
            if not val_title and not val_url:
                target_row_idx = idx + 1
                break

    new_rows = []
    current_idx = target_row_idx
    for job in jobs_list:
        # Try to get the prefilled index from column A
        existing_num = ""
        if current_idx - 1 < len(all_rows):
            existing_num = all_rows[current_idx - 1][0].strip() if len(all_rows[current_idx - 1]) > 0 else ""
            
        if existing_num.isdigit():
            row_num = int(existing_num)
        else:
            # Calculate ordinal number from the previous row
            row_num = 1
            if new_rows:
                row_num = new_rows[-1][0] + 1
            else:
                for r in reversed(all_rows[:current_idx - 1]):
                    if r and len(r) > 0 and str(r[0]).isdigit():
                        row_num = int(r[0]) + 1
                        break
                        
        row_data = [
            row_num, 
            job["title"], 
            job["url"], 
            datetime.now().strftime("%d.%m.%Y"), 
            "Pending", 
            "", 
            "" # Screenshot link is empty
        ]
        new_rows.append(row_data)
        current_idx += 1
        
    # Batch update all rows in a single operation
    sheet.update(range_name=f"A{target_row_idx}:G{target_row_idx + len(new_rows) - 1}", values=new_rows)
    return [row[0] for row in new_rows]

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_user_allowed(message.from_user):
        logger.warning(f"Unauthorized access attempt from ID {message.from_user.id} (@{message.from_user.username})")
        return
    db.register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.reply(
        "Hello! Send me a message containing a job link and a screenshot of the application, and I'll record it in the table!",
        reply_markup=get_main_menu_keyboard()
    )

@dp.message(Command("train"))
@dp.message(F.text == "🧠 Train ML Model")
async def cmd_train(message: types.Message):
    if not is_user_allowed(message.from_user):
        return
    status_msg = await message.reply("Training ML model...")
    try:
        # Train in a separate thread since scikit-learn training blocks the event loop
        result_msg = await asyncio.to_thread(ml.train_model)
        await status_msg.edit_text(result_msg)
    except Exception as e:
        logger.error(f"Error during model training: {e}")
        await status_msg.edit_text(f"An error occurred: {e}")

@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Cancel")
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.reply("No active action to cancel.", reply_markup=get_main_menu_keyboard())
        return
    await state.clear()
    await message.reply("Action cancelled. The bot has returned to normal mode.", reply_markup=get_main_menu_keyboard())



def format_and_split_leads(jobs_list):
    """
    Formats the list of vacancies.
    Returns a list of messages (each < 4000 chars) to stay within Telegram limits.
    """
    chunks = []
    current_chunk = ""
    for idx, job in enumerate(jobs_list):
        source_name = "Kwork" if job["source"] == "kwork" else "FL.ru"
        price_str = f" [{job['price']}]" if job.get("price") else ""
        
        # Escape HTML characters for Telegram HTML compatibility
        safe_title = html.escape(job['title'])
        safe_url = html.escape(job['url'])
        safe_price = html.escape(price_str)
        
        item_text = (
            f"<b>{idx + 1}. {safe_title} / {source_name} /{safe_price}</b>\n"
            f"- {safe_url}\n\n"
        )
        if len(current_chunk) + len(item_text) > 4000:
            chunks.append(current_chunk)
            current_chunk = item_text
        else:
            current_chunk += item_text
            
    if current_chunk:
        chunks.append(current_chunk)
        
    return chunks

async def send_leads_to_user(event, state: FSMContext, jobs_list):
    """
    Sends the list of vacancies to the user, splitting it if Telegram limits are exceeded.
    Attaches the "Approve all" button only to the last message.
    """
    chunks = format_and_split_leads(jobs_list)
    if not chunks:
        if isinstance(event, types.Message):
            await event.reply("The vacancy list is empty.")
        else:
            await event.message.answer("The vacancy list is empty.")
        await state.clear()
        return
        
    # Create keyboard with two buttons
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Approve all", callback_data="approve_all"))
    builder.add(InlineKeyboardButton(text="🔄 Replace some", callback_data="replace_part"))
    builder.adjust(2)
    markup = builder.as_markup()
    
    # Save vacancies to FSM state
    await state.update_data(current_leads=jobs_list)
    await state.set_state(LeadStates.reviewing)
    
    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        reply_markup = markup if is_last else None
        
        if isinstance(event, types.Message):
            await event.answer(chunk, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await event.message.answer(chunk, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("get_leads"))
@dp.message(F.text == "📋 Get Leads")
@dp.message(F.text == "⚡ Get Leads (Force)")
async def cmd_get_leads(message: types.Message, state: FSMContext):
    if not is_user_allowed(message.from_user):
        logger.warning(f"Unauthorized access attempt from ID {message.from_user.id} (@{message.from_user.username})")
        return
        
    db.register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    # Check command arguments (force)
    args = message.text.split()
    force_mode = (len(args) > 1 and args[1].lower() == "force") or message.text == "⚡ Get Leads (Force)"
    
    # Check how many new vacancies are available
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM parsed_jobs WHERE matched = 1 AND status = 'new'")
    available_count = cursor.fetchone()["cnt"]
    conn.close()
    
    if available_count == 0:
        await message.reply("There are no new matched vacancies in the database yet. Please wait for the parser to collect them.")
        return
        
    if available_count < 25 and not force_mode:
        await message.reply(
            f"There are not 25 new vacancies in the database yet (currently available: {available_count}).\n"
            f"Please wait for the parser to collect enough, or run "
            f"<code>/get_leads force</code> to fetch all available vacancies right now.",
            parse_mode="HTML"
        )
        return
        
    # Extract vacancies
    limit = min(25, available_count)
    jobs = db.get_new_matched_jobs(limit)
    
    # Update their status to 'shown' in DB
    ext_ids = [j["external_id"] for j in jobs]
    db.update_jobs_status(ext_ids, "shown")
    
    # Send to user
    await send_leads_to_user(message, state, jobs)

@dp.callback_query(F.data == "replace_part")
async def process_replace_part(callback_query: CallbackQuery, state: FSMContext):
    if not is_user_allowed(callback_query.from_user):
        try:
            await callback_query.answer("Access denied", show_alert=True)
        except Exception as e:
            logger.warning(f"Failed to answer callback query: {e}")
        return
        
    try:
        await callback_query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        
    await state.set_state(LeadStates.waiting_for_replace)
    await callback_query.message.answer(
        "Send the numbers of vacancies to replace (e.g. <code>2, 5, 12-15</code>):",
        parse_mode="HTML"
    )

@dp.message(LeadStates.waiting_for_replace)
async def handle_leads_replace(message: types.Message, state: FSMContext):
    if not is_user_allowed(message.from_user):
        return
        
    text = message.text.strip()
    if text == "❌ Cancel" or text.lower() == "/cancel":
        await state.clear()
        await message.reply("Action cancelled. The bot has returned to normal mode.", reply_markup=get_main_menu_keyboard())
        return

    indices = parse_indices(text)
    if not indices:
        await message.reply(
            "Invalid input format. Send numbers separated by spaces or commas; ranges can be used (e.g. <code>2, 5, 12-15</code>):",
            parse_mode="HTML"
        )
        return
        
    # Retrieve current vacancies from state
    data = await state.get_data()
    current_leads = data.get("current_leads", [])
    
    if not current_leads:
        await message.reply("The vacancy list in memory is empty. Please request new ones using /get_leads.")
        await state.clear()
        return
        
    # Validate indices
    invalid_indices = [idx for idx in indices if idx < 1 or idx > len(current_leads)]
    if invalid_indices:
        await message.reply(
            f"Invalid vacancy numbers: {invalid_indices}. "
            f"Numbers must be between 1 and {len(current_leads)}."
        )
        return
        
    # We need to replace K vacancies
    k = len(indices)
    
    # Check if there are new vacancies in DB for replacement
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM parsed_jobs WHERE matched = 1 AND status = 'new'")
    db_available_count = cursor.fetchone()["cnt"]
    conn.close()
    
    # Vacancies rejected by the user
    rejected_jobs = [current_leads[idx - 1] for idx in indices]
    rejected_ext_ids = [j["external_id"] for j in rejected_jobs]
    
    # Mark rejected ones as 'rejected' in the database
    db.update_jobs_status(rejected_ext_ids, "rejected")
    
    # Extract new vacancies for replacement
    replacements = []
    if db_available_count > 0:
        limit = min(k, db_available_count)
        replacements = db.get_new_matched_jobs(limit)
        # Mark new ones as 'shown'
        replacement_ext_ids = [r["external_id"] for r in replacements]
        db.update_jobs_status(replacement_ext_ids, "shown")
        
    # Build new list: replace rejected vacancies with new ones
    new_leads = list(current_leads)
    
    # Assemble replacement map
    replacement_map = {}
    for idx_in_list, rep in zip(indices, replacements):
        replacement_map[idx_in_list - 1] = rep
        
    final_leads = []
    for idx, job in enumerate(current_leads):
        if idx in replacement_map:
            final_leads.append(replacement_map[idx])
        elif idx in [i - 1 for i in indices]:
            # Item was rejected but no replacements were available (DB empty) - skip
            pass
        else:
            final_leads.append(job)
            
    replaced_count = len([idx for idx in indices if idx - 1 in replacement_map])
    removed_count = k - replaced_count
    
    msg_parts = []
    if replaced_count > 0:
        msg_parts.append(f"Replaced vacancies: {replaced_count}")
    if removed_count > 0:
        msg_parts.append(f"Removed without replacement (DB empty): {removed_count}")
        
    await message.reply(f"Updating the list. {', '.join(msg_parts)}")
    
    # Send the updated list
    await send_leads_to_user(message, state, final_leads)

@dp.callback_query(F.data == "approve_all")
async def process_approve_all(callback_query: CallbackQuery, state: FSMContext):
    if not is_user_allowed(callback_query.from_user):
        try:
            await callback_query.answer("Access denied", show_alert=True)
        except Exception as e:
            logger.warning(f"Failed to answer callback query: {e}")
        return
        
    # Get vacancies from state
    data = await state.get_data()
    current_leads = data.get("current_leads", [])
    
    if not current_leads:
        try:
            await callback_query.answer("List is empty or expired.", show_alert=True)
        except Exception as e:
            logger.warning(f"Failed to answer callback query: {e}")
        return
        
    try:
        await callback_query.answer("Adding vacancies to Google Sheet...")
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
    
    # Mark vacancies as 'approved' in DB
    ext_ids = [j["external_id"] for j in current_leads]
    await asyncio.to_thread(db.update_jobs_status, ext_ids, "approved")
    
    # Write to Google Sheet in one batch
    try:
        row_numbers = await asyncio.to_thread(add_vacancies_to_sheet, current_leads)
        
        await callback_query.message.answer(
            f"Successfully added {len(current_leads)} vacancies to Google Sheet!\n"
            f"Row range: {row_numbers[0]} - {row_numbers[-1]}.\n"
            f"Vacancy status is set to <b>\"Pending\"</b>.",
            parse_mode="HTML"
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error during batch write to Google Sheet: {e}")
        await callback_query.message.answer(f"An error occurred while writing to the sheet: {e}")

@dp.message()
async def handle_report(message: types.Message):
    # Ignore message if the user list is not empty and sender ID is not allowed
    if not is_user_allowed(message.from_user):
        logger.warning(f"Ignoring message from unauthorized user ID {message.from_user.id} (@{message.from_user.username})")
        return

    db.register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    text = message.text or message.caption or ""
    text = text.strip()
    
    # Search for URLs in the text
    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        await message.reply("Could not find a job link in the message.", reply_markup=get_main_menu_keyboard())
        return

    vacancy_url = urls[0]
    screenshot_formula_or_url = ""

    status_message = await message.reply("One second, writing to the table...")

    # Photos are no longer processed by the bot
    screenshot_formula_or_url = urls[1] if len(urls) > 1 else ""

    # Parse the title in a separate thread (CPU/IO blocking)
    title = await asyncio.to_thread(fetch_vacancy_title, vacancy_url)
    date_str = datetime.now().strftime("%d.%m.%Y")

    try:
        # Write to Google Sheets in a separate thread (Network/IO blocking)
        row_num = await asyncio.to_thread(
            add_vacancy_to_sheet, 
            title, 
            vacancy_url, 
            date_str, 
            screenshot_formula_or_url
        )
        await status_message.edit_text(f"Done! Added under #{row_num}:\n\"{title}\"")
        logger.info(f"Successfully added row #{row_num}: '{title}'")
        
    except Exception as e:
        logger.error(f"Error writing to Google Sheets: {e}")
        await status_message.edit_text(f"Error writing to Google Sheets: {e}")

async def main():
    db.init_db()
    if not bot:
        logger.error("Bot is not initialized. Please check your .env configuration!")
        return
    logger.info("Starting Telegram bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
