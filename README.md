# Telegram Vacancy Tracker Bot

Этот бот собирает ссылки на вакансии из Telegram-сообщений, автоматически парсит заголовок страницы (название вакансии) и вносит данные в Google Таблицу.

## 📋 Требования
- Python 3.10+
- Токен Telegram-бота (полученный через [@BotFather](https://t.me/BotFather))
- Сервисный аккаунт Google Cloud (для работы с Google Таблицами)

---

## 🛠 Настройка и установка

### Шаг 1. Клонирование и настройка окружения
1. Перейдите в папку проекта:
   ```bash
   cd /Users/thereal_vadim/.gemini/antigravity/scratch/tg_vacancy_tracker
   ```
2. Создайте виртуальное окружение и активируйте его:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
3. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```

### Шаг 2. Конфигурация `.env`
1. Скопируйте шаблон файла конфигурации:
   ```bash
   cp .env.example .env
   ```
2. Откройте файл `.env` и укажите ваши настройки:
   * `TG_TOKEN`: Ваш токен бота от `@BotFather`.
   * `SPREADSHEET_ID`: ID вашей Google таблицы (длинная строка из URL-адреса таблицы).
   * `ALLOWED_USERS`: ID пользователей Telegram через запятую, которым разрешено пользоваться ботом (вы можете узнать свой ID у бота вроде `@userinfobot`).
   * `CREDENTIALS_FILE`: Имя файла ключа авторизации (по умолчанию `credentials.json`).

### Шаг 3. Получение credentials.json (Google Sheets API)
1. Перейдите в [Google Cloud Console](https://console.cloud.google.com/).
2. Создайте новый проект (или выберите существующий).
3. Перейдите в раздел **APIs & Services > Library** и включите:
   - **Google Sheets API**
   - **Google Drive API**
4. Перейдите в раздел **APIs & Services > Credentials**.
5. Нажмите **Create Credentials** и выберите **Service Account**.
6. Заполните данные сервисного аккаунта и нажмите **Create**.
7. В списке сервисных аккаунтов нажмите на только что созданный, перейдите во вкладку **Keys**, нажмите **Add Key > Create new key** и выберите формат **JSON**.
8. Файл скачается автоматически. Переименуйте его в `credentials.json` и перенесите в корневую папку бота (`/Users/thereal_vadim/.gemini/antigravity/scratch/tg_vacancy_tracker/credentials.json`).
9. **Важно:** Откройте этот файл, скопируйте `client_email` (email сервисного аккаунта) и предоставьте этому email доступ (Share/Поделиться) к вашей Google Таблице с правами "Редактор" (Editor).

---

## 🚀 Запуск
Убедитесь, что виртуальное окружение активировано, и запустите бота:
```bash
python3 bot.py
```

---

## 📝 Использование
Бот ожидает сообщения, содержащие ссылки.
- Первая найденная ссылка в сообщении считается **ссылкой на вакансию**.
- Вторая найденная ссылка считается **ссылкой на скриншот** (например, ссылка на Telegra.ph, Lightshot или imgur с подтверждением отклика).

Пример сообщения боту:
```text
https://career.habr.com/vacancies/1000123456
https://imgur.com/a/some-screenshot-id
```

Бот спарсит заголовок страницы по первой ссылке (например, "Python Developer в Google"), найдет свободную строчку после 10-й строки таблицы и вставит запись со следующими колонками:
`[№, Название вакансии, Ссылка на вакансию, Дата, "Отклик ушел", Комментарий, Ссылка на скриншот]`
