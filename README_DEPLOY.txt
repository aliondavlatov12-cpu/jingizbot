VortenaBot — corrected deployment package

Files in this package:
- bot.py
- sender.py
- db.py
- config.py
- requirements.txt
- .env.example
- README_DEPLOY.txt

Render deployment:
1. Upload/replace the project files in the repository.
2. Set the Render start command to: python bot.py
3. Set environment variables: BOT_TOKEN, API_ID, API_HASH, DATABASE_URL, SUPER_ADMINS.
4. Keep API_HASH, BOT_TOKEN, database credentials, and Telegram session strings private.
5. Deploy and inspect logs after pressing “Подключить аккаунт”.

Telegram login code notes:
- The bot now reports the delivery type returned by Telegram (app, SMS, call, etc.).
- Telegram chooses the delivery method; a successful API request does not guarantee an SMS or service-chat message was delivered.
- Enter the Telegram login code first. The 2FA password prompt appears only after Telegram accepts the login code and reports that 2FA is required.
- The bot logs code length and masked phone suffix only; it does not log the code or 2FA password.
- Use international phone format, e.g. +992XXXXXXXXX or +7XXXXXXXXXX. Ambiguous 10-digit numbers are no longer silently treated as Russian numbers.

Auto-broadcast:
- A campaign with an unauthorized Telegram session is stopped and the owner is asked to reconnect, instead of retrying the same permanent session error every interval.
- Transient errors can still be retried after the configured interval.

Validation performed for this package:
- Python AST parsing and py_compile succeeded for bot.py, sender.py, db.py, and config.py.
- This is static validation only; live Telegram login and PostgreSQL behavior must be verified with the actual Render environment and credentials.


Login resend fix (2026-10-12):
- The resend button no longer rejects locally just because Telegram omitted next_type. After the Telegram timeout, it asks Telegram's auth.resendCode endpoint and displays the actual server error if no alternate delivery method is available.
- SentCodeTypeApp means Telegram selected another already-authorized Telegram session, not SMS. This application cannot force Telegram to deliver an SMS if Telegram does not offer it.
- Run only one polling instance for this bot token; duplicate getUpdates polling can cause TelegramConflictError and in-memory login-state loss.
- See AUDIT_LOGIN_FIX_RU.txt for the comparison and validation limits.


Последние исправления проверки:
- Кнопка главного меню «⏱ Интервал» теперь открывает меню настройки интервала; старый вариант «⏳ Интервал» тоже поддерживается.
- Повторный запрос кода использует Telethon auth.ResendCodeRequest и не блокируется локально только из-за отсутствующего next_type.
- Запуск: python bot.py. В Render должен работать только один экземпляр polling с одним BOT_TOKEN.
