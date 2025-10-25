import asyncio
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile, InlineKeyboardButton, \
    InlineKeyboardMarkup
from typing import Optional

from .config import config
from .funpay_client import FunPayClient


def build_menu() -> ReplyKeyboardMarkup:
	rows = [
		[KeyboardButton(text="▶️ Старт"), KeyboardButton(text="⏹ Стоп")],
		[KeyboardButton(text="💬 Пресеты"), KeyboardButton(text="📊 Статистика")],
		[KeyboardButton(text="📦 Активные заказы"), KeyboardButton(text="📋 Список чатов")],
		[KeyboardButton(text="🔐 Войти FunPay"), KeyboardButton(text="🔁 Автоответ Вкл/Выкл")],
		[KeyboardButton(text="💬 Прямой чат Вкл/Выкл"), KeyboardButton(text="❓ Помощь")],
		[KeyboardButton(text="🔍 Анализ валюты"), KeyboardButton(text="👤 Анализ аккаунтов")],
		[KeyboardButton(text="✅ Автоответ Вкл"), KeyboardButton(text="❌ Автоответ Выкл")],
		[KeyboardButton(text="📊 Статус автоответа")],
	]
	return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_presets_menu() -> ReplyKeyboardMarkup:
	rows = [[KeyboardButton(text=p)] for p in config.preset_replies()[:10]]
	rows.append([KeyboardButton(text="⬅️ Назад")])
	return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


class Controller:
	def __init__(self, client: FunPayClient, admin_id_ref, bot: Bot) -> None:
		self.client = client
		self._admin_id_ref = admin_id_ref
		self._presets_mode = False
		self._direct_chat_mode = False
		self._selected_dialog = None  # node_id выбранного диалога
		self._bot = bot  # Сохраняем ссылку на бота для отправки скриншотов
	
	async def send_screenshot_to_admin(self, screenshot_path: str, dialog_info: Optional[dict]) -> None:
		"""Отправляет скриншот администратору"""
		try:
			admin_id = self._admin_id_ref("get")
			if admin_id == 0:
				return
			
			# Формируем текст сообщения
			if dialog_info:
				caption = f"Новое сообщение от: {dialog_info.get('username', 'Неизвестно')}"
			else:
				caption = "Новое сообщение в FunPay"
			
			# Отправляем скриншот
			photo = FSInputFile(screenshot_path)
			await self._bot.send_photo(chat_id=admin_id, photo=photo, caption=caption)
			print(f"[Telegram] Скриншот отправлен администратору")
		except Exception as e:
			print(f"[Telegram] Ошибка отправки скриншота: {e}")

	async def cmd_start(self, message: Message) -> None:
		admin_id = self._admin_id_ref("get")
		if admin_id == 0 and message.from_user:
			admin_id = message.from_user.id
			self._admin_id_ref("set", admin_id)
			await message.answer("Вы назначены администратором этого бота.")
		await message.answer(
			"Меню управления FunPay ботом:",
			reply_markup=build_menu(),
		)

	async def handle_buttons(self, message: Message) -> None:
		text = (message.text or "").strip()
		if text == "▶️ Старт":
			await self.client.start()
			await message.answer("Автопостер запущен.")
		elif text == "⏹ Стоп":
			await self.client.stop()
			await message.answer("Автопостер остановлен.")
		elif text == "📊 Статистика":
			bal = await self.client.fetch_balance()
			trade = await self.client.fetch_trade_totals()
			active = await self.client.fetch_active_orders(limit=10)
			parts = []
			if bal:
				parts.append(f"Баланс: {bal}")
			if trade:
				parts.append(
					"Активные/закрытые заказы:"\
					f"\n• Оплачено: {trade['paid_count']} на {trade['paid_sum']} ₽"\
					f"\n• Закрыто: {trade['closed_count']} на {trade['closed_sum']} ₽"\
					f"\n• Возвраты: {trade['refund_count']} на {trade['refund_sum']} ₽"\
					f"\nИтого по списку: {trade['total_sum']} ₽"
				)
			if active:
				lines = ["\nОткрытые (Оплачен):"]
				for o in active:
					lines.append(f"• {o['order_id']} | {o['buyer']} | {o['amount']} ₽ | {o['status']}")
				parts.append("\n".join(lines))
			if not parts:
				parts.append("Не удалось получить статистику")
			await message.answer("\n\n".join(parts))
		elif text == "💬 Пресеты":
			self._presets_mode = True
			await message.answer("Выберите пресет сообщения или пришлите свой текст:", reply_markup=build_presets_menu())
		elif text == "⬅️ Назад":
			self._presets_mode = False
			self._selected_dialog = None
			await message.answer("Меню:", reply_markup=build_menu())
		elif text == "📋 Список чатов":
			dialogs = await self.client.get_unread_dialogs()
			if not dialogs:
				await message.answer("Нет диалогов или ошибка загрузки")
				return
			msg = "Выберите диалог (отправьте номер):\n\n"
			for i, d in enumerate(dialogs, 1):
				unread_mark = "🔴" if d["unread"] else "⚪"
				msg += f"{i}. {unread_mark} {d['name']}\n"
			await message.answer(msg)
			self._selected_dialog = None  # Сбросим выбор
		elif text == "📦 Активные заказы":
			orders = await self.client.fetch_active_orders(limit=20)
			if not orders:
				await message.answer("Открытых заказов нет")
				return
			lines = [f"Активные заказы (Оплачен): {len(orders)}"]
			for o in orders:
				amount_raw = o.get("amount")
				if isinstance(amount_raw, str) and ("₽" in amount_raw or "RUB" in amount_raw):
					amount = amount_raw
				elif amount_raw:
					amount = f"{amount_raw} ₽"
				else:
					amount = "—"
				buyer = o.get("buyer") or "—"
				order_id = o.get("order_id") or "—"
				lines.append(f"• {buyer} — {amount} ({order_id})")
			await message.answer("\n".join(lines))
		elif text == "✉️ Ответить в непрочитанный":
			sent = await self.client.reply_first_unread(config.auto_reply_text)
			await message.answer("Отправлено" if sent else "Непрочитанных диалогов нет или ошибка")
		elif text == "🔁 Автоответ Вкл/Выкл":
			from .config import config as cfg
			cfg.auto_reply_enabled = not cfg.auto_reply_enabled
			await message.answer(f"Автоответ: {'включен' if cfg.auto_reply_enabled else 'выключен'}")
		elif text == "💬 Прямой чат Вкл/Выкл":
			self._direct_chat_mode = not self._direct_chat_mode
			status = "включен ✅" if self._direct_chat_mode else "выключен ⛔"
			target = ""
			if self._direct_chat_mode and self._selected_dialog:
				target = "\n\nВыбран конкретный диалог. Все сообщения идут туда."
			elif self._direct_chat_mode:
				target = "\n\nДиалог не выбран — сообщения идут в первый непрочитанный."
			await message.answer(f"Прямой чат: {status}{target}")
		elif text == "❓ Помощь":
			await self.cmd_help(message)
		elif text == "🔍 Анализ валюты":
			await self.cmd_analyze_currency(message)
		elif text == "👤 Анализ аккаунтов":
			await self.cmd_analyze_accounts(message)
		elif text == "✅ Автоответ Вкл":
			await self.cmd_auto_on(message)
		elif text == "❌ Автоответ Выкл":
			await self.cmd_auto_off(message)
		elif text == "📊 Статус автоответа":
			await self.cmd_auto_status(message)
		elif text == "🔐 Войти FunPay":
			creds = self.client.load_saved_credentials()
			if not creds:
				await message.answer(
					"Сначала задайте логин/пароль:\n/fp_set ваш_логин ваш_пароль\n\n"
					"Или вставьте cookie:\n/fp_cookie COOKIE_СТРОКА\n\n"
					"Либо открой окно логина для ручного ввода: /fp_cookiebp"
				)
				return
			success = await self.client.login_with_credentials(creds.get("login", ""), creds.get("password", ""))
			await message.answer("Вход выполнен ✅" if success else "Не удалось войти ❌\nПроверьте логин/пароль через /fp_set или попробуйте /fp_cookie")
		else:
			if self._presets_mode and text:
				from .config import config as cfg
				cfg.auto_reply_text = text
				await message.answer("Текст автоответа обновлён. Нажмите '✉️ Ответить в непрочитанный' для отправки.")
			elif self._direct_chat_mode and text:
				# Если выбран диалог, пишем в него, иначе в первый непрочитанный
				if self._selected_dialog:
					sent = await self.client.reply_to_dialog(self._selected_dialog, text)
				else:
					sent = await self.client.reply_first_unread(text)
				await message.answer("✅ Отправлено" if sent else "❌ Ошибка отправки")
			elif text and text.isdigit():
				# Пользователь выбрал номер диалога
				dialogs = await self.client.get_unread_dialogs()
				idx = int(text) - 1
				if 0 <= idx < len(dialogs):
					self._selected_dialog = dialogs[idx]["node_id"]
					await message.answer(f"Выбран диалог: {dialogs[idx]['name']}\n\nВключите 'Прямой чат' и пишите — всё пойдёт в этот диалог.")
				else:
					await message.answer("Неверный номер диалога")

	async def cmd_text(self, message: Message) -> None:
		parts = (message.text or "").split(maxsplit=1)
		if len(parts) < 2:
			await message.answer("Использование: /text ваш_текст")
			return
		self.client.set_post_text(parts[1])
		await message.answer("Текст автопоста обновлён.")

	async def cmd_interval(self, message: Message) -> None:
		parts = (message.text or "").split(maxsplit=1)
		if len(parts) < 2:
			await message.answer("Использование: /interval минуты")
			return
		try:
			minutes = int(parts[1])
		except ValueError:
			await message.answer("Нужно число минут.")
			return
		self.client.set_interval_minutes(minutes)
		await message.answer(f"Интервал обновлён: {minutes} мин.")

	async def cmd_services_interval(self, message: Message) -> None:
		"""Установить интервал отправки в услуги в секундах"""
		parts = (message.text or "").split(maxsplit=1)
		if len(parts) < 2:
			await message.answer("Использование: /services_interval [секунды]")
			return
		try:
			seconds = int(parts[1])
			if seconds < 5:
				await message.answer("Интервал должен быть больше 5 секунд")
				return
			
			from .config import config
			config.services_interval = seconds
			await message.answer(f"Интервал услуг установлен: {seconds} секунд")
		except ValueError:
			await message.answer("Неверный формат числа")

	async def cmd_fp_set(self, message: Message) -> None:
		parts = (message.text or "").split(maxsplit=2)
		if len(parts) < 3:
			await message.answer("Использование: /fp_set логин пароль\n(логин — это username, не email)")
			return
		login, password = parts[1], parts[2]
		from json import dumps
		from pathlib import Path
		Path("storage").mkdir(parents=True, exist_ok=True)
		Path("storage/credentials.json").write_text(dumps({"login": login, "password": password}), encoding="utf-8")
		await message.answer("Данные сохранены. Нажмите '🔐 Войти FunPay' или используйте /fp_login.")

	async def cmd_fp_login(self, message: Message) -> None:
		parts = (message.text or "").split(maxsplit=2)
		if len(parts) < 3:
			await message.answer("Использование: /fp_login логин пароль\n(логин — это username, не email)")
			return
		success = await self.client.login_with_credentials(parts[1], parts[2])
		await message.answer("Вход выполнен ✅" if success else "Не удалось войти ❌")

	async def cmd_fp_cookie(self, message: Message) -> None:
		parts = (message.text or "").split(maxsplit=1)
		if len(parts) < 2:
			await message.answer("Использование: /fp_cookie cookie_header (строка из заголовка Cookie)")
			return
		success = await self.client.login_with_cookie_header(parts[1])
		await message.answer("Cookies применены" if success else "Не удалось применить cookies")

	async def cmd_text_scchat(self, message: Message) -> None:
		"""Показать скриншоты личных чатов"""
		await message.answer("Делаю скриншоты личных чатов...")
		try:
			screenshots = await self.client.get_chat_screenshots()
			if screenshots:
				await message.answer("📸 Отправляю скриншоты чатов...")
				for i, screenshot_path in enumerate(screenshots, 1):
					try:
						photo = FSInputFile(screenshot_path)
						await message.answer_photo(
							photo=photo, 
							caption=f"Чат {i}: {screenshot_path.split('/')[-1]}"
						)
					except Exception as e:
						await message.answer(f"❌ Ошибка отправки скриншота {i}: {e}")
				await message.answer("✅ Все скриншоты чатов отправлены!")
			else:
				await message.answer("❌ Ошибка создания скриншотов чатов")
		except Exception as e:
			await message.answer(f"❌ Ошибка: {e}")

	async def cmd_test_sc(self, message: Message) -> None:
		"""Показать скриншот процесса отправки в чат услуг"""
		await message.answer("Делаю скриншот процесса отправки в чат услуг...")
		try:
			# Отправляем тестовое сообщение с сохранением скриншотов
			screenshots = await self.client.send_message_with_screenshot()
			if screenshots:
				await message.answer("📸 Отправляю скриншоты процесса...")
				for i, screenshot_path in enumerate(screenshots, 1):
					try:
						photo = FSInputFile(screenshot_path)
						await message.answer_photo(
							photo=photo, 
							caption=f"Скриншот {i}/5: {screenshot_path.split('/')[-1]}"
						)
					except Exception as e:
						await message.answer(f"❌ Ошибка отправки скриншота {i}: {e}")
				await message.answer("✅ Все скриншоты отправлены!")
			else:
				await message.answer("❌ Ошибка создания скриншотов")
		except Exception as e:
			await message.answer(f"❌ Ошибка: {e}")

	async def cmd_auto_test(self, message: Message) -> None:
		"""Тест автоответа - отправить тестовое сообщение в первый диалог"""
		await message.answer("Тестирую автоответ...")
		try:
			success = await self.client.test_auto_reply()
			if success:
				await message.answer("✅ Тестовый автоответ отправлен!")
			else:
				await message.answer("❌ Не удалось отправить автоответ")
		except Exception as e:
			await message.answer(f"❌ Ошибка: {e}")

	async def cmd_auto_force(self, message: Message) -> None:
		"""Принудительно отправить автоответ в любой диалог"""
		await message.answer("Принудительно отправляю автоответ...")
		try:
			# Получаем список диалогов
			dialogs = await self.client.get_unread_dialogs()
			if not dialogs:
				await message.answer("❌ Нет доступных диалогов")
				return
			
			# Берём первый диалог
			dialog = dialogs[0]
			await message.answer(f"Отправляю автоответ в диалог: {dialog['name']}")
			
			# Отправляем автоответ
			success = await self.client.test_auto_reply()
			if success:
				await message.answer("✅ Автоответ отправлен!")
			else:
				await message.answer("❌ Не удалось отправить автоответ")
		except Exception as e:
			await message.answer(f"❌ Ошибка: {e}")

	async def cmd_auto_restart(self, message: Message) -> None:
		"""Перезапустить автоответ"""
		await message.answer("Перезапускаю автоответ...")
		try:
			# Останавливаем бота
			await self.client.stop()
			await asyncio.sleep(2)
			
			# Запускаем заново
			await self.client.start()
			await message.answer("✅ Автоответ перезапущен!")
		except Exception as e:
			await message.answer(f"❌ Ошибка: {e}")

	async def cmd_auto_clear(self, message: Message) -> None:
		"""Очистить список обработанных диалогов"""
		await message.answer("Очищаю список обработанных диалогов...")
		try:
			import time
			current_time = time.time()
			# Удаляем только старые диалоги (старше 5 минут)
			old_dialogs = []
			for dialog_id, timestamp in self.client._processed_dialogs.items():
				if isinstance(timestamp, (int, float)) and current_time - timestamp > 300:  # 5 минут
					old_dialogs.append(dialog_id)
			
			for dialog_id in old_dialogs:
				del self.client._processed_dialogs[dialog_id]
			
			# Сохраняем обновленный список
			self.client._save_processed_dialogs()
			
			await message.answer(f"✅ Очищено {len(old_dialogs)} старых диалогов!")
		except Exception as e:
			await message.answer(f"❌ Ошибка: {e}")

	async def cmd_auto_clear_all(self, message: Message) -> None:
		"""Очистить ВСЕ обработанные диалоги"""
		await message.answer("Очищаю ВСЕ обработанные диалоги...")
		try:
			self.client._processed_dialogs.clear()
			# Удаляем файл с обработанными диалогами
			import os
			if os.path.exists(self.client._processed_dialogs_file):
				os.remove(self.client._processed_dialogs_file)
			await message.answer("✅ ВСЕ обработанные диалоги очищены!")
		except Exception as e:
			await message.answer(f"❌ Ошибка: {e}")

	async def cmd_auto_stop(self, message: Message) -> None:
		"""Экстренная остановка автоответа"""
		await message.answer("🚨 ЭКСТРЕННАЯ ОСТАНОВКА АВТООТВЕТА!")
		try:
			# Выключаем автоответ
			from .config import config
			config.auto_reply_enabled = False
			
			# Останавливаем бота
			await self.client.stop()
			
			# Очищаем обработанные диалоги
			self.client._processed_dialogs.clear()
			
			await message.answer("✅ Автоответ остановлен и очищен!")
		except Exception as e:
			await message.answer(f"❌ Ошибка: {e}")

	async def cmd_analyze_currency(self, message: Message) -> None:
		"""Анализ цен на валюту Minecraft"""
		await message.answer("🔍 Анализирую цены на валюту Minecraft...")
		try:
			result = await self.client.analyze_currency_prices()
			if result:
				await message.answer(result)
			else:
				await message.answer("❌ Не удалось получить данные о ценах")
		except Exception as e:
			await message.answer(f"❌ Ошибка анализа цен: {e}")

	async def cmd_analyze_accounts(self, message: Message) -> None:
		"""Главное меню анализа аккаунтов"""
		keyboard = InlineKeyboardMarkup(inline_keyboard=[
			[InlineKeyboardButton(text="🛒 Купить аккаунт", callback_data="analyze_buy")],
			[InlineKeyboardButton(text="💰 Продать аккаунт", callback_data="analyze_sell")]
		])
		
		await message.answer("🎯 **Выберите действие:**", reply_markup=keyboard)

	async def handle_analyze_buy(self, callback_query) -> None:
		"""Обработка выбора покупки"""
		await callback_query.answer()
		
		keyboard = InlineKeyboardMarkup(inline_keyboard=[
			[InlineKeyboardButton(text="👑 Герцог навсегда", callback_data="b_г")],
			[InlineKeyboardButton(text="👑 Князь навсегда", callback_data="b_к")],
			[InlineKeyboardButton(text="👑 Глава", callback_data="b_гл")],
			[InlineKeyboardButton(text="👑 Титан", callback_data="b_т")],
			[InlineKeyboardButton(text="👑 Элита", callback_data="b_э")],
			[InlineKeyboardButton(text="👑 Принц", callback_data="b_п")]
		])
		
		await callback_query.message.edit_text("🛒 **Выберите донат для покупки:**", reply_markup=keyboard)

	async def handle_analyze_sell(self, callback_query) -> None:
		"""Обработка выбора продажи"""
		await callback_query.answer()
		
		keyboard = InlineKeyboardMarkup(inline_keyboard=[
			[InlineKeyboardButton(text="👑 Герцог навсегда", callback_data="s_г")],
			[InlineKeyboardButton(text="👑 Князь навсегда", callback_data="s_к")],
			[InlineKeyboardButton(text="👑 Глава", callback_data="s_гл")],
			[InlineKeyboardButton(text="👑 Титан", callback_data="s_т")],
			[InlineKeyboardButton(text="👑 Элита", callback_data="s_э")],
			[InlineKeyboardButton(text="👑 Принц", callback_data="s_п")]
		])
		
		await callback_query.message.edit_text("💰 **Выберите донат для продажи:**", reply_markup=keyboard)

	async def handle_buy_donate(self, callback_query) -> None:
		"""Обработка покупки конкретного доната"""
		await callback_query.answer()
		
		# Парсим данные: b_к
		donate_code = callback_query.data.split("_")[1]
		
		donate_map = {
			"г": "герцог",
			"к": "князь", 
			"гл": "глава",
			"т": "титан",
			"э": "элита",
			"п": "принц"
		}
		donate_name = donate_map.get(donate_code, "князь")
		
		donate_display = {
			"герцог": "Герцог навсегда",
			"князь": "Князь навсегда", 
			"глава": "Глава",
			"титан": "Титан",
			"элита": "Элита",
			"принц": "Принц"
		}
		
		display_name = donate_display.get(donate_name, donate_name)
		
		# Показываем меню выбора типа привязки
		keyboard = InlineKeyboardMarkup(inline_keyboard=[
			[InlineKeyboardButton(text="🔗 С привязкой", callback_data=f"bt_{donate_name}_w")],
			[InlineKeyboardButton(text="🔓 Без привязки", callback_data=f"bt_{donate_name}_wo")],
			[InlineKeyboardButton(text="❓ Утерянная привязка", callback_data=f"bt_{donate_name}_l")],
			[InlineKeyboardButton(text="🔄 Любой тип", callback_data=f"bt_{donate_name}_a")]
		])
		
		await callback_query.message.edit_text(f"🛒 **Выберите тип привязки для {display_name}:**", reply_markup=keyboard)

	async def handle_buy_type(self, callback_query) -> None:
		"""Обработка выбора типа привязки для покупки"""
		await callback_query.answer()
		
		# Парсим данные: bt_князь_w
		parts = callback_query.data.split("_")
		donate_name = parts[1]
		binding_code = parts[2]
		
		# Преобразуем код в тип
		binding_map = {
			"w": "with",
			"wo": "without", 
			"l": "lost",
			"a": "any"
		}
		binding_type = binding_map.get(binding_code, "any")
		
		donate_display = {
			"герцог": "Герцог навсегда",
			"князь": "Князь навсегда", 
			"глава": "Глава",
			"титан": "Титан",
			"элита": "Элита",
			"принц": "Принц"
		}
		
		binding_display = {
			"with": "с привязкой",
			"without": "без привязки", 
			"lost": "с утерянной привязкой",
			"any": "любого типа"
		}
		
		display_name = donate_display.get(donate_name, donate_name)
		binding_name = binding_display.get(binding_type, binding_type)
		
		await callback_query.message.edit_text(f"🛒 Ищу самый дешевый аккаунт **{display_name}** {binding_name}...")
		
		try:
			result = await self.client.find_cheapest_account_with_binding(donate_name, binding_type)
			if result:
				await callback_query.message.edit_text(result)
			else:
				await callback_query.message.edit_text("❌ Не удалось найти аккаунты для покупки")
		except Exception as e:
			await callback_query.message.edit_text(f"❌ Ошибка поиска: {e}")

	async def handle_sell_donate(self, callback_query) -> None:
		"""Обработка продажи конкретного доната"""
		await callback_query.answer()
		
		# Парсим данные: s_к
		donate_code = callback_query.data.split("_")[1]
		
		donate_map = {
			"г": "герцог",
			"к": "князь", 
			"гл": "глава",
			"т": "титан",
			"э": "элита",
			"п": "принц"
		}
		donate_name = donate_map.get(donate_code, "князь")
		
		donate_display = {
			"герцог": "Герцог навсегда",
			"князь": "Князь навсегда", 
			"глава": "Глава",
			"титан": "Титан",
			"элита": "Элита",
			"принц": "Принц"
		}
		
		display_name = donate_display.get(donate_name, donate_name)
		await callback_query.message.edit_text(f"💰 Анализирую цены для продажи **{display_name}**...")
		
		try:
			result = await self.client.analyze_sell_price(donate_name)
			if result:
				await callback_query.message.edit_text(result)
			else:
				await callback_query.message.edit_text("❌ Не удалось проанализировать цены для продажи")
		except Exception as e:
			await callback_query.message.edit_text(f"❌ Ошибка анализа: {e}")

	async def cmd_analyze_lot(self, message: Message) -> None:
		"""Анализ конкретного лота по ссылке"""
		# Извлекаем URL из сообщения
		text = message.text or ""
		url = text.replace("/analyze_lot", "").strip()
		
		if not url:
			await message.answer("❌ Укажите ссылку на лот после команды:\n/analyze_lot https://funpay.com/lot/12345/")
			return
		
		if "funpay.com" not in url:
			await message.answer("❌ Ссылка должна быть с FunPay")
			return
		
		await message.answer(f"🔍 Анализирую лот: {url}")
		
		try:
			result = await self.client.analyze_lot_details(url)
			if result:
				await message.answer(result)
			else:
				await message.answer("❌ Не удалось проанализировать лот")
		except Exception as e:
			await message.answer(f"❌ Ошибка анализа: {e}")

	async def handle_donate_callback(self, callback_query) -> None:
		"""Обработка выбора доната"""
		await callback_query.answer()
		
		donate_name = callback_query.data.replace("donate_", "")
		donate_display = {
			"герцог": "Герцог навсегда",
			"князь": "Князь навсегда", 
			"глава": "Глава",
			"титан": "Титан",
			"элита": "Элита",
			"принц": "Принц"
		}
		
		display_name = donate_display.get(donate_name, donate_name)
		await callback_query.message.edit_text(f"🔍 Анализирую цены на аккаунты с донатом **{display_name}**...")
		
		try:
			result = await self.client.analyze_account_prices(donate_name)
			if result:
				await callback_query.message.edit_text(result)
			else:
				await callback_query.message.edit_text("❌ Не удалось получить данные о ценах")
		except Exception as e:
			await callback_query.message.edit_text(f"❌ Ошибка анализа цен: {e}")

	async def cmd_test(self, message: Message) -> None:
		"""Тестовая команда с Groq (Llama)"""
		await message.answer("🤖 Тестирую Groq (Llama)...")
		try:
			import requests
			
			# Groq API (бесплатный)
			api_key = "gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # Нужен ключ от Groq
			url = "https://api.groq.com/openai/v1/chat/completions"
			
			# Заголовки
			headers = {
				"Content-Type": "application/json",
				"Authorization": f"Bearer {api_key}"
			}
			
			# Данные для запроса
			data = {
				"model": "llama-3.1-8b-instant",
				"messages": [
					{"role": "system", "content": "You are a helpful assistant."},
					{"role": "user", "content": "Привет! Это тест API ключа. Ответь коротко."}
				],
				"max_tokens": 100,
				"temperature": 0.7
			}
			
			# Отправляем запрос
			response = requests.post(url, headers=headers, json=data)
			result = response.json()
			
			if 'choices' in result and len(result['choices']) > 0:
				answer = result['choices'][0]['message']['content']
				await message.answer(f"✅ Groq (Llama) отвечает: {answer}")
			else:
				await message.answer(f"❌ Ошибка API: {result}")
			
		except Exception as e:
			await message.answer(f"❌ Ошибка теста: {e}")


	async def cmd_auto_on(self, message: Message) -> None:
		"""Включить автоответ"""
		from .config import config
		config.auto_reply_enabled = True
		await message.answer("✅ Автоответ включён!")

	async def cmd_auto_off(self, message: Message) -> None:
		"""Выключить автоответ"""
		from .config import config
		config.auto_reply_enabled = False
		await message.answer("❌ Автоответ выключен!")

	async def cmd_auto_status(self, message: Message) -> None:
		"""Показать статус автоответа"""
		from .config import config
		status = "включён ✅" if config.auto_reply_enabled else "выключен ❌"
		text = config.auto_reply_text[:50] + "..." if len(config.auto_reply_text) > 50 else config.auto_reply_text
		running = "работает 🟢" if self.client.running else "остановлен 🔴"
		await message.answer(f"Автоответ: {status}\nБот: {running}\nТекст: {text}")

	async def cmd_help(self, message: Message) -> None:
		"""Показать список всех команд"""
		help_text = """
🤖 **Список команд FunPay бота:**

**📊 Основные команды:**
/start - Запустить бота и показать меню
/help - Показать этот список команд

**💰 Информация:**
/balance - Показать баланс FunPay
/orders - Показать активные заказы
/totals - Показать статистику торгов

**⚙️ Настройки:**
/text [текст] - Установить текст для автоответа
/interval [минуты] - Установить интервал автоответа
/services_interval [секунды] - Установить интервал отправки в услуги

**🔐 Авторизация FunPay:**
/fp_set [логин] [пароль] - Установить логин/пароль
/fp_login - Войти в FunPay с сохранёнными данными
/fp_cookie [cookie] - Войти через cookie
/fp_cookiebp - Открыть браузер для ручного входа
/fp_reset - Полный сброс сессии FunPay

**📸 Скриншоты:**
/text_scchat - Скриншоты чатов
/test_sc - Скриншоты отправки в услугах
/auto_test - Тест автоответа

**🔄 Автоответ:**
/auto_on - Включить автоответ
/auto_off - Выключить автоответ
/auto_status - Статус автоответа
/auto_force - Принудительно отправить автоответ
/auto_restart - Перезапустить автоответ
/auto_clear - Очистить старые диалоги (старше 5 мин)
/auto_clear_all - Очистить ВСЕ диалоги
/auto_stop - 🚨 ЭКСТРЕННАЯ ОСТАНОВКА

**🔍 Анализ цен:**
/analyze_currency - Анализ цен на валюту Minecraft
/analyze_accounts - Анализ аккаунтов (покупка/продажа)
/analyze_lot - Анализ конкретного лота по ссылке

**🤖 Тестирование:**
/test - Тест Groq (Llama) - нужен API ключ

**💡 Подсказки:**
- Автоответ работает постоянно и отвечает на все сообщения
- В услугах бот пишет постоянно с заданным интервалом в секундах
- Нажми "Старт" один раз - бот будет писать в услуги автоматически
- Все команды работают только для администратора
		"""
		await message.answer(help_text)


async def run_telegram(client: FunPayClient) -> None:
	if not config.telegram_bot_token:
		print("[TG] TELEGRAM_BOT_TOKEN не задан — Telegram-бот отключён")
		return

	storage_dir = Path("storage")
	storage_dir.mkdir(parents=True, exist_ok=True)
	admin_file = storage_dir / "admin_id.txt"

	admin_id = config.telegram_admin_id
	if admin_id == 0 and admin_file.exists():
		try:
			admin_id = int(admin_file.read_text(encoding="utf-8").strip() or "0")
		except Exception:
			admin_id = 0

	def admin_id_ref(action: str, value: Optional[int] = None):
		nonlocal admin_id
		if action == "get":
			return admin_id
		if action == "set" and value is not None:
			admin_id = int(value)
			try:
				admin_file.write_text(str(admin_id), encoding="utf-8")
			except Exception:
				pass
			return admin_id
		return admin_id

	bot = Bot(token=config.telegram_bot_token)
	dp = Dispatcher()
	controller = Controller(client, admin_id_ref, bot)
	
	# Устанавливаем коллбэк для отправки скриншотов
	client.set_screenshot_callback(controller.send_screenshot_to_admin)

	def only_admin(handler):
		async def wrapper(message: Message, *args, **kwargs):
			current_admin = admin_id_ref("get")
			if current_admin == 0 and message.text and message.text.startswith("/start"):
				return await handler(message)
			if message.from_user and message.from_user.id == current_admin:
				return await handler(message)
			await message.answer("Доступ запрещён")
		return wrapper

	# Команды сначала
	dp.message.register(only_admin(controller.cmd_start), Command("start"))
	dp.message.register(only_admin(controller.cmd_help), Command("help"))
	dp.message.register(only_admin(controller.cmd_text), Command("text"))
	dp.message.register(only_admin(controller.cmd_interval), Command("interval"))
	dp.message.register(only_admin(controller.cmd_services_interval), Command("services_interval"))
	dp.message.register(only_admin(controller.cmd_fp_set), Command("fp_set"))
	dp.message.register(only_admin(controller.cmd_fp_login), Command("fp_login"))
	dp.message.register(only_admin(controller.cmd_fp_cookie), Command("fp_cookie"))
	dp.message.register(only_admin(controller.cmd_text_scchat), Command("text_scchat"))
	dp.message.register(only_admin(controller.cmd_test_sc), Command("test_sc"))
	dp.message.register(only_admin(controller.cmd_auto_test), Command("auto_test"))
	dp.message.register(only_admin(controller.cmd_auto_force), Command("auto_force"))
	dp.message.register(only_admin(controller.cmd_auto_restart), Command("auto_restart"))
	dp.message.register(only_admin(controller.cmd_auto_clear), Command("auto_clear"))
	dp.message.register(only_admin(controller.cmd_auto_clear_all), Command("auto_clear_all"))
	dp.message.register(only_admin(controller.cmd_auto_stop), Command("auto_stop"))
	dp.message.register(only_admin(controller.cmd_auto_on), Command("auto_on"))
	dp.message.register(only_admin(controller.cmd_auto_off), Command("auto_off"))
	dp.message.register(only_admin(controller.cmd_auto_status), Command("auto_status"))
	dp.message.register(only_admin(controller.cmd_analyze_currency), Command("analyze_currency"))
	dp.message.register(only_admin(controller.cmd_analyze_accounts), Command("analyze_accounts"))
	dp.message.register(only_admin(controller.cmd_analyze_lot), Command("analyze_lot"))
	dp.message.register(only_admin(controller.cmd_test), Command("test"))
	
	# Обработчики кнопок анализа
	@dp.callback_query(lambda c: c.data == "analyze_buy")
	async def handle_analyze_buy(callback_query):
		await controller.handle_analyze_buy(callback_query)

	@dp.callback_query(lambda c: c.data == "analyze_sell")
	async def handle_analyze_sell(callback_query):
		await controller.handle_analyze_sell(callback_query)

	@dp.callback_query(lambda c: c.data.startswith("b_"))
	async def handle_buy_donate(callback_query):
		await controller.handle_buy_donate(callback_query)

	@dp.callback_query(lambda c: c.data.startswith("s_"))
	async def handle_sell_donate(callback_query):
		await controller.handle_sell_donate(callback_query)

	@dp.callback_query(lambda c: c.data.startswith("bt_"))
	async def handle_buy_type(callback_query):
		await controller.handle_buy_type(callback_query)

	# Обработчик кнопок донатов (старый)
	@dp.callback_query(lambda c: c.data.startswith("donate_"))
	async def handle_donate_callback(callback_query):
		await controller.handle_donate_callback(callback_query)

	@dp.message(Command("fp_cookiebp"))
	async def _cmd_fp_cookiebp(message: Message):
		current_admin = admin_id_ref("get")
		if not message.from_user or message.from_user.id != current_admin:
			return await message.answer("Доступ запрещён")
		await message.answer("Открываю окно браузера для ручного входа...")
		success = await client.open_login_browser()
		if success:
			await message.answer("Окно открыто. Введите логин/пароль, решите капчу и нажмите Войти. После входа просто нажмите любую кнопку в меню бота.")
		else:
			await message.answer("Не удалось открыть окно входа.")

	@dp.message(Command("fp_reset"))
	async def _cmd_fp_reset(message: Message):
		current_admin = admin_id_ref("get")
		if not message.from_user or message.from_user.id != current_admin:
			return await message.answer("Доступ запрещён")
		await message.answer("Сбрасываю сессию...")
		success = await client.reset_session()
		if success:
			await message.answer("Сессия сброшена. Запустите снова: кнопка '🔐 Войти FunPay' для новой авторизации.")
		else:
			await message.answer("Не удалось сбросить сессию.")
	# Потом кнопки/текст
	dp.message.register(only_admin(controller.handle_buttons), F.text & ~F.text.startswith("/"))

	try:
		await bot.delete_webhook(drop_pending_updates=True)
	except Exception:
		pass

	await dp.start_polling(bot)
