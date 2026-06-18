import os
import re
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
from aiogram.types import InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
import db
from utils import parse_indices

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")

# Парсинг списка разрешенных пользователей (ID или юзернеймы)
ALLOWED_IDS = set()
ALLOWED_USERNAMES = set()
for item in ALLOWED_USERS_RAW.split(","):
    item = item.strip()
    if not item:
        continue
    if item.isdigit():
        ALLOWED_IDS.add(int(item))
    else:
        # Убираем символ @, если он указан
        username = item.lstrip("@").lower()
        if username:
            ALLOWED_USERNAMES.add(username)

def is_user_allowed(user: types.User) -> bool:
    # Если списки разрешенных пользователей пусты, доступ открыт для всех
    if not ALLOWED_IDS and not ALLOWED_USERNAMES:
        return True
    if user.id in ALLOWED_IDS:
        return True
    if user.username and user.username.lower() in ALLOWED_USERNAMES:
        return True
    return False

if not TG_TOKEN:
    logger.error("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения TG_TOKEN не задана!")
if not SPREADSHEET_ID:
    logger.error("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения SPREADSHEET_ID не задана!")

bot = Bot(token=TG_TOKEN) if TG_TOKEN else None
dp = Dispatcher()

class LeadStates(StatesGroup):
    reviewing = State()
    waiting_for_replace = State()

# Подключение к Google Таблицам (синхронная вспомогательная функция)
def get_google_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(f"Файл учетных данных '{CREDENTIALS_FILE}' не найден. Пожалуйста, следуйте инструкциям в README.md.")
    
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    workbook = client.open_by_key(SPREADSHEET_ID)
    # Берем самый последний лист (актуальный отчет)
    return workbook.worksheets()[-1]

# Получение OAuth2 токена доступа к Google API (для загрузки файлов)
def get_drive_access_token():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(f"Файл учетных данных '{CREDENTIALS_FILE}' не найден.")
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    creds.refresh(Request())
    return creds.token

# Загрузка файла в Google Drive и открытие доступа по ссылке
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
        
        # Делаем файл доступным для чтения по ссылке
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
        logger.error(f"Ошибка при загрузке в Google Drive: {e}")
        return None

# Извлечение тега title с биржи (синхронная функция, будет выполняться в thread pool)
def fetch_vacancy_title(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.string if soup.title else "Новый заказ"
            # Очистка заголовка от лишней информации бирж
            title = re.sub(r"\s*—\s*Кворк.*|\s*—\s*фриланс.*|\s*\|.*", "", title, flags=re.IGNORECASE)
            return title.strip()
        else:
            logger.warning(f"Не удалось загрузить страницу {url}, статус: {response.status_code}")
    except Exception as e:
        logger.error(f"Ошибка парсинга тайтла для {url}: {e}")
    return "Заказ (не удалось спарсить название)"

# Запись строки в Google Таблицу (синхронная функция, будет выполняться в thread pool)
def add_vacancy_to_sheet(title, vacancy_url, date_str, screenshot_url):
    sheet = get_google_sheet()
    all_rows = sheet.get_all_values()
    
    # Ищем первую пустую строку (где нет названия вакансии и ссылки) начиная с 10-й строки (индекс 9)
    target_row_idx = None
    for idx, row in enumerate(all_rows):
        if idx >= 9:
            val_title = row[1].strip() if len(row) > 1 else ""
            val_url = row[2].strip() if len(row) > 2 else ""
            if not val_title and not val_url:
                target_row_idx = idx + 1  # 1-based index в gspread
                break

    if target_row_idx is not None:
        # Если строка уже существует в шаблоне (например, с предзаполненным номером в колонке А)
        existing_num = all_rows[target_row_idx - 1][0].strip() if len(all_rows[target_row_idx - 1]) > 0 else ""
        if existing_num.isdigit():
            row_num = int(existing_num)
        else:
            # Вычисляем номер строки по предыдущей
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
            "Отклик ушел", 
            "", 
            screenshot_url
        ]
        
        # Обновляем существующую строку
        sheet.update(range_name=f"A{target_row_idx}:G{target_row_idx}", values=[new_row])
    else:
        # Если свободных предзаполненных строк нет, вычисляем номер и добавляем новую строку в конец
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
            "Отклик ушел", 
            "", 
            screenshot_url
        ]
        
        next_index = len(all_rows) + 1
        sheet.insert_row(new_row, next_index)
        
    return row_num

# Запись нескольких строк в Google Таблицу одной пачкой (для одобренных из пачки)
def add_vacancies_to_sheet(jobs_list):
    sheet = get_google_sheet()
    all_rows = sheet.get_all_values()
    
    # Находим индекс первой пустой строчки под номерами (после 10-й строки)
    target_row_idx = len(all_rows) + 1
    for idx, row in enumerate(all_rows):
        if idx >= 9: # Строка 10 (0-indexed)
            val_title = row[1].strip() if len(row) > 1 else ""
            val_url = row[2].strip() if len(row) > 2 else ""
            if not val_title and not val_url:
                target_row_idx = idx + 1
                break

    new_rows = []
    current_idx = target_row_idx
    for job in jobs_list:
        # Пытаемся получить предустановленный номер № из колонки А
        existing_num = ""
        if current_idx - 1 < len(all_rows):
            existing_num = all_rows[current_idx - 1][0].strip() if len(all_rows[current_idx - 1]) > 0 else ""
            
        if existing_num.isdigit():
            row_num = int(existing_num)
        else:
            # Вычисляем порядковый номер по предыдущей строке
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
            "Ожидает отклика", 
            "", 
            "" # Ссылка на скриншот пустая
        ]
        new_rows.append(row_data)
        current_idx += 1
        
    # Записываем все строки одной операцией update
    sheet.update(range_name=f"A{target_row_idx}:G{target_row_idx + len(new_rows) - 1}", values=new_rows)
    return [row[0] for row in new_rows]

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_user_allowed(message.from_user):
        logger.warning(f"Неавторизованный доступ от ID {message.from_user.id} (@{message.from_user.username})")
        return
    db.register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.reply("Привет! Отправь мне сообщение с ссылкой на вакансию и скриншот отклика, и я запишу её в таблицу!")

@dp.message()
async def handle_report(message: types.Message):
    # Если список пользователей не пустой и ID отправителя нет в списке, игнорируем
    if not is_user_allowed(message.from_user):
        logger.warning(f"Игнорируем сообщение от неразрешенного пользователя ID {message.from_user.id} (@{message.from_user.username})")
        return

    db.register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    text = message.text or message.caption or ""
    text = text.strip()
    
    # Ищем ссылки в тексте
    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        await message.reply("Не нашел ссылки на вакансию в сообщении.")
        return

    vacancy_url = urls[0]
    screenshot_formula_or_url = ""

    status_message = await message.reply("Секунду, заношу в таблицу...")

    # Если к сообщению прикреплено фото
    if message.photo:
        try:
            # Берем фото самого высокого качества
            photo = message.photo[-1]
            temp_filename = f"temp_{message.message_id}.png"
            temp_path = os.path.join(os.path.dirname(__file__), temp_filename)
            
            # Скачиваем файл во временную папку проекта
            await bot.download(photo, destination=temp_path)
            
            # Загружаем в Google Drive (в фоновом потоке)
            file_id = await asyncio.to_thread(upload_file_to_drive, temp_path, temp_filename)
            
            # Удаляем временный файл
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
            if file_id:
                # Вставляем формулу для отображения картинки прямо в ячейке
                screenshot_formula_or_url = f'=IMAGE("https://drive.google.com/uc?export=view&id={file_id}")'
            else:
                # Если Google Drive API не включен, бот об этом сообщит в процессе работы
                await status_message.edit_text("Не удалось загрузить фото на Google Drive (убедитесь, что Drive API включен в консоли). Записываю без фото...")
        except Exception as e:
            logger.error(f"Ошибка при обработке и загрузке фото: {e}")
            await status_message.edit_text(f"Ошибка при обработке фото: {e}. Продолжаю без фото...")
    else:
        # Если фото нет, проверяем вторую ссылку в сообщении
        screenshot_formula_or_url = urls[1] if len(urls) > 1 else ""

    # Выполняем парсинг тайтла в отдельном потоке (CPU/IO blocking)
    title = await asyncio.to_thread(fetch_vacancy_title, vacancy_url)
    date_str = datetime.now().strftime("%d.%m.%Y")

    try:
        # Записываем в Google Sheets в отдельном потоке (Network/IO blocking)
        row_num = await asyncio.to_thread(
            add_vacancy_to_sheet, 
            title, 
            vacancy_url, 
            date_str, 
            screenshot_formula_or_url
        )
        await status_message.edit_text(f"Готово! Добавлено под №{row_num}:\n«{title}»")
        logger.info(f"Успешно добавлена строка #{row_num}: '{title}'")
        
    except Exception as e:
        logger.error(f"Ошибка при записи в Google Sheets: {e}")
        await status_message.edit_text(f"Ошибка при записи в Google Sheets: {e}")

def format_and_split_leads(jobs_list):
    """
    Форматирует список вакансий.
    Возвращает список сообщений (каждое < 4000 символов), чтобы избежать лимита Telegram.
    """
    chunks = []
    current_chunk = ""
    for idx, job in enumerate(jobs_list):
        source_name = "Kwork" if job["source"] == "kwork" else "FL.ru"
        price_str = f" [{job['price']}]" if job.get("price") else ""
        item_text = (
            f"<b>{idx + 1}. {job['title']} / {source_name} /{price_str}</b>\n"
            f"- {job['url']}\n\n"
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
    Отправляет пользователю список вакансий, разбивая на части при превышении лимита длины.
    Кнопку "Approve all" прикрепляет только к последнему сообщению.
    """
    chunks = format_and_split_leads(jobs_list)
    if not chunks:
        if isinstance(event, types.Message):
            await event.reply("Список вакансий пуст.")
        else:
            await event.message.answer("Список вакансий пуст.")
        await state.clear()
        return
        
    # Создаем клавиатуру с двумя кнопками
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Подтвердить все", callback_data="approve_all"))
    builder.add(InlineKeyboardButton(text="🔄 Заменить часть", callback_data="replace_part"))
    builder.adjust(2)
    markup = builder.as_markup()
    
    # Сохраняем вакансии в состоянии FSM
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
async def cmd_get_leads(message: types.Message, state: FSMContext):
    if not is_user_allowed(message.from_user):
        logger.warning(f"Неавторизованный доступ от ID {message.from_user.id} (@{message.from_user.username})")
        return
        
    db.register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    # Проверяем аргументы команды (force)
    args = message.text.split()
    force_mode = len(args) > 1 and args[1].lower() == "force"
    
    # Проверяем сколько новых вакансий доступно
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM parsed_jobs WHERE matched = 1 AND status = 'new'")
    available_count = cursor.fetchone()["cnt"]
    conn.close()
    
    if available_count == 0:
        await message.reply("В базе данных пока нет новых подходящих вакансий. Пожалуйста, подождите, пока парсер соберет их.")
        return
        
    if available_count < 25 and not force_mode:
        await message.reply(
            f"В базе данных пока нет 25 новых вакансий (всего доступно: {available_count}).\n"
            f"Пожалуйста, подождите, пока парсер соберет достаточное количество, или выполните "
            f"<code>/get_leads force</code>, чтобы получить все доступные вакансии прямо сейчас.",
            parse_mode="HTML"
        )
        return
        
    # Извлекаем вакансии
    limit = min(25, available_count)
    jobs = db.get_new_matched_jobs(limit)
    
    # Обновляем их статус на 'shown' в БД
    ext_ids = [j["external_id"] for j in jobs]
    db.update_jobs_status(ext_ids, "shown")
    
    # Отправляем пользователю
    await send_leads_to_user(message, state, jobs)

@dp.callback_query(F.data == "replace_part")
async def process_replace_part(callback_query: CallbackQuery, state: FSMContext):
    if not is_user_allowed(callback_query.from_user):
        await callback_query.answer("Доступ запрещен", show_alert=True)
        return
        
    await callback_query.answer()
    await state.set_state(LeadStates.waiting_for_replace)
    await callback_query.message.answer(
        "Пришлите номера вакансий для замены (например: <code>2, 5, 12-15</code>):",
        parse_mode="HTML"
    )

@dp.message(LeadStates.waiting_for_replace)
async def handle_leads_replace(message: types.Message, state: FSMContext):
    if not is_user_allowed(message.from_user):
        return
        
    text = message.text.strip()
    indices = parse_indices(text)
    if not indices:
        await message.reply(
            "Неверный формат ввода. Пришлите номера через пробел или запятую, можно использовать диапазоны (например: <code>2, 5, 12-15</code>):",
            parse_mode="HTML"
        )
        return
        
    # Получаем текущие вакансии из состояния
    data = await state.get_data()
    current_leads = data.get("current_leads", [])
    
    if not current_leads:
        await message.reply("Список вакансий в памяти пуст. Пожалуйста, запросите новые с помощью /get_leads.")
        await state.clear()
        return
        
    # Проверяем валидность индексов
    invalid_indices = [idx for idx in indices if idx < 1 or idx > len(current_leads)]
    if invalid_indices:
        await message.reply(
            f"Неверные номера вакансий: {invalid_indices}. "
            f"Номера должны быть от 1 до {len(current_leads)}."
        )
        return
        
    # Нам нужно заменить K вакансий
    k = len(indices)
    
    # Проверяем, есть ли новые вакансии в БД для замены
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM parsed_jobs WHERE matched = 1 AND status = 'new'")
    db_available_count = cursor.fetchone()["cnt"]
    conn.close()
    
    # Вакансии, которые пользователь отклонил
    rejected_jobs = [current_leads[idx - 1] for idx in indices]
    rejected_ext_ids = [j["external_id"] for j in rejected_jobs]
    
    # Помечаем отклоненные как 'rejected' в БД
    db.update_jobs_status(rejected_ext_ids, "rejected")
    
    # Извлекаем новые вакансии для замены
    replacements = []
    if db_available_count > 0:
        limit = min(k, db_available_count)
        replacements = db.get_new_matched_jobs(limit)
        # Помечаем новые как 'shown'
        replacement_ext_ids = [r["external_id"] for r in replacements]
        db.update_jobs_status(replacement_ext_ids, "shown")
        
    # Строим новый список вакансий: заменяем отклоненные на новые
    new_leads = list(current_leads)
    
    # Собираем замененную мапу
    replacement_map = {}
    for idx_in_list, rep in zip(indices, replacements):
        replacement_map[idx_in_list - 1] = rep
        
    final_leads = []
    for idx, job in enumerate(current_leads):
        if idx in replacement_map:
            final_leads.append(replacement_map[idx])
        elif idx in [i - 1 for i in indices]:
            # Элемент был отклонен, но замены для него не хватило (база пуста) - пропускаем
            pass
        else:
            final_leads.append(job)
            
    replaced_count = len([idx for idx in indices if idx - 1 in replacement_map])
    removed_count = k - replaced_count
    
    msg_parts = []
    if replaced_count > 0:
        msg_parts.append(f"Заменено вакансий: {replaced_count}")
    if removed_count > 0:
        msg_parts.append(f"Удалено без замены (база пуста): {removed_count}")
        
    await message.reply(f"Обновляю список. {', '.join(msg_parts)}")
    
    # Отправляем обновленный список
    await send_leads_to_user(message, state, final_leads)

@dp.callback_query(F.data == "approve_all")
async def process_approve_all(callback_query: CallbackQuery, state: FSMContext):
    if not is_user_allowed(callback_query.from_user):
        await callback_query.answer("Доступ запрещен", show_alert=True)
        return
        
    # Получаем вакансии из состояния
    data = await state.get_data()
    current_leads = data.get("current_leads", [])
    
    if not current_leads:
        await callback_query.answer("Список пуст или устарел.", show_alert=True)
        return
        
    await callback_query.answer("Заношу вакансии в Google Таблицу...")
    
    # Помечаем вакансии как 'approved' в БД
    ext_ids = [j["external_id"] for j in current_leads]
    await asyncio.to_thread(db.update_jobs_status, ext_ids, "approved")
    
    # Записываем в Google Таблицу одной пачкой
    try:
        row_numbers = await asyncio.to_thread(add_vacancies_to_sheet, current_leads)
        
        await callback_query.message.answer(
            f"Успешно добавлено {len(current_leads)} вакансий в Google Таблицу!\n"
            f"Диапазон номеров строк: {row_numbers[0]} - {row_numbers[-1]}.\n"
            f"Статус вакансий установлен в <b>«Ожидает отклика»</b>.",
            parse_mode="HTML"
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка при пакетной записи в Google Таблицу: {e}")
        await callback_query.message.answer(f"Произошла ошибка при записи в таблицу: {e}")

async def main():
    db.init_db()
    if not bot:
        logger.error("Бот не инициализирован. Проверьте конфигурацию .env!")
        return
    logger.info("Запуск Telegram бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
