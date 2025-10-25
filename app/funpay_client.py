import asyncio
import os
import json
import time
import re
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .config import config


CREDENTIALS_PATH = Path("storage/credentials.json")


class FunPayClient:
	def __init__(self) -> None:
		self._browser: Optional[Browser] = None
		self._context: Optional[BrowserContext] = None
		self._page: Optional[Page] = None
		self._orders_page: Optional[Page] = None
		self._running: bool = False
		self._post_text: str = config.post_text
		self._post_interval_sec: int = max(60, config.post_interval_minutes * 60)
		self._last_unread_count: int = 0  # Для отслеживания новых сообщений
		self._screenshot_callback = None  # Коллбэк для отправки скриншотов в TG
		# Кэши для ускорения ответов в Telegram
		self._cached_balance: Optional[str] = None
		self._cached_balance_ts: float = 0.0
		self._cached_trade_totals: Optional[dict] = None
		self._cached_trade_totals_ts: float = 0.0
		self._cached_active_orders: Optional[list] = None
		self._cached_active_orders_ts: float = 0.0
		# Убрано отслеживание обработанных услуг - бот должен писать постоянно
		self._processed_dialogs: dict = {}  # Отслеживаем обработанные диалоги навсегда
		self._processed_dialogs_file = "storage/processed_dialogs.json"

	@property
	def running(self) -> bool:
		return self._running

	def set_post_text(self, text: str) -> None:
		self._post_text = text

	def set_interval_minutes(self, minutes: int) -> None:
		self._post_interval_sec = max(60, minutes * 60)
	
	def set_screenshot_callback(self, callback) -> None:
		"""Устанавливает коллбэк для отправки скриншотов в Telegram"""
		self._screenshot_callback = callback

	async def launch(self, force_headful: bool = False) -> None:
		Path(os.path.dirname(config.storage_path) or ".").mkdir(parents=True, exist_ok=True)
		pw = await async_playwright().start()
		
		# Проверяем, есть ли сохранённая сессия
		storage_state = None
		has_session = Path(config.storage_path).exists()
		if has_session:
			storage_state = config.storage_path
			print("[FunPay] Найдена сохранённая сессия — запуск в headless режиме")
		else:
			print("[FunPay] Сессии нет — открываю браузер для входа")
			print("[FunPay] ИНСТРУКЦИЯ: Войдите в FunPay в открывшемся браузере, затем нажмите любую кнопку в Telegram-боте")
		
		# Если сессии нет - открываем браузер с головой для ручного входа
		# Если сессия есть - работаем в headless режиме, если не принудительно headful
		headless_mode = (config.headless if has_session else False) and not force_headful
		
		# Запускаем браузер с параметрами, чтобы обойти детекцию ботов
		self._browser = await pw.chromium.launch(
			headless=headless_mode,
			args=[
				'--disable-blink-features=AutomationControlled',
				'--disable-dev-shm-usage',
				'--no-sandbox',
				'--disable-setuid-sandbox',
				'--disable-web-security',
				'--disable-features=IsolateOrigins,site-per-process'
			]
		)
		
		# Контекст с "человеческими" параметрами
		self._context = await self._browser.new_context(
			storage_state=storage_state,
			viewport={'width': 1920, 'height': 1080},
			user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
			locale='ru-RU',
			timezone_id='Europe/Moscow',
			permissions=['geolocation'],
			extra_http_headers={
				'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
				'Accept-Encoding': 'gzip, deflate, br',
				'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8'
			}
		)

		# Блокируем тяжёлые ресурсы для ускорения загрузки страниц, но НЕ блокируем reCAPTCHA
		async def _route_filter(route):
			try:
				req = route.request
				url = req.url.lower()
				rt = req.resource_type
				# НЕ блокируем reCAPTCHA и связанные с ней ресурсы
				if any(h in url for h in ["recaptcha", "google.com/recaptcha", "gstatic.com/recaptcha"]):
					return await route.continue_()
				# Блокируем остальные тяжёлые ресурсы
				if rt in ["image", "font", "media"] or any(h in url for h in [
					"googletagmanager", "google-analytics", "mc.yandex", "doubleclick"
				]):
					return await route.abort()
				return await route.continue_()
			except Exception:
				try:
					await route.continue_()
				except Exception:
					pass
		await self._context.route("**/*", _route_filter)
		
		self._page = await self._context.new_page()
		
		# Убираем признаки автоматизации
		await self._page.add_init_script("""
			Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
			window.navigator.chrome = {runtime: {}};
			Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
			Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
		""")
		
		await self._page.goto(config.funpay_section_url, wait_until="domcontentloaded")

		# Создаём несколько вкладок для разных задач
		self._orders_page = None
		self._chat_page = None
		self._services_page = None
		self._finance_page = None
		
		try:
			# Вкладка для заказов
			self._orders_page = await self._context.new_page()
			await self._orders_page.goto(config.funpay_base_url + "orders/trade?state=paid", wait_until="domcontentloaded")
			
			# Вкладка для чатов
			self._chat_page = await self._context.new_page()
			await self._chat_page.goto("https://funpay.com/chat/", wait_until="domcontentloaded")
			
			# Вкладка для услуг (автопост)
			self._services_page = await self._context.new_page()
			await self._services_page.goto(config.funpay_section_url, wait_until="domcontentloaded")
			
			# Вкладка для финансов
			self._finance_page = await self._context.new_page()
			await self._finance_page.goto(config.funpay_base_url + "account/finance", wait_until="domcontentloaded")
			
			print("[FunPay] Открыто 4 вкладки для быстрой работы")
		except Exception as e:
			print(f"[FunPay] Ошибка создания вкладок: {e}")

		# Предзагрузка кешей, чтобы первые ответы были быстрыми
		asyncio.create_task(self.fetch_balance())
		asyncio.create_task(self.fetch_trade_totals())
		asyncio.create_task(self.fetch_active_orders())

	async def open_login_browser(self) -> bool:
		"""Открыть браузер с окном логина FunPay (принудительно headful)."""
		try:
			# Остановим все процессы
			await self.stop()
			# Перезапустим браузер в headful-режиме, чтобы показать окно
			if self._browser:
				try:
					await self._browser.close()
				except Exception:
					pass
			self._browser = None
			self._context = None
			self._page = None
			self._orders_page = None
			await self.launch(force_headful=True)
			if not self._page:
				return False
			print("[FunPay] Открываю страницу логина для ручного ввода")
			await self._page.goto(config.funpay_base_url + "account/login", wait_until="domcontentloaded")
			print("[FunPay] Введите логин и пароль в открывшемся окне, решите капчу и нажмите Войти")
			return True
		except Exception as e:
			print(f"[FunPay] Ошибка открытия окна логина: {e}")
			return False

	async def _ensure_orders_page(self) -> Page:
		if not self._orders_page or self._orders_page.is_closed():
			if not self._context:
				await self.launch()
			self._orders_page = await self._context.new_page()
		await self._orders_page.goto(config.funpay_base_url + "orders/trade?state=paid", wait_until="domcontentloaded")
		return self._orders_page

	async def close(self) -> None:
		self._running = False
		if self._context:
			await self._context.storage_state(path=config.storage_path)
			print(f"[FunPay] Сессия сохранена в {config.storage_path}")
		if self._browser:
			await self._browser.close()
		self._browser = None
		self._context = None
		self._page = None

	async def reset_session(self) -> bool:
		"""Полный сброс сессии: закрыть браузер, удалить storage и кеши."""
		try:
			# Остановим процессы и закроем браузер
			await self.stop()
			if self._browser:
				try:
					await self._browser.close()
				except Exception:
					pass
			self._browser = None
			self._context = None
			self._page = None
			self._orders_page = None
			# Удалим файлы состояния
			try:
				Path(config.storage_path).unlink(missing_ok=True)
			except Exception:
				pass
			try:
				Path("storage/funpay.json").unlink(missing_ok=True)
			except Exception:
				pass
			try:
				Path("storage/credentials.json").unlink(missing_ok=True)
			except Exception:
				pass
			# Сброс кешей
			self._cached_balance = None
			self._cached_trade_totals = None
			self._cached_active_orders = None
			self._cached_balance_ts = 0.0
			self._cached_trade_totals_ts = 0.0
			self._cached_active_orders_ts = 0.0
			print("[FunPay] Сессия и учётные данные сброшены")
			return True
		except Exception as e:
			print(f"[FunPay] Ошибка сброса сессии: {e}")
			return False

	async def login_with_credentials(self, login: str, password: str) -> bool:
		"""Вход по логину (username) и паролю - РУЧНОЙ режим с ожиданием"""
		if not self._page:
			return False
		try:
			print(f"[FunPay] Открываю страницу входа для логина: {login}")
			await self._page.goto(config.funpay_base_url + "account/login", wait_until="domcontentloaded")
			await self._page.wait_for_timeout(2000)
			
			# Селектор для поля логина (username)
			login_sel = 'input[name="login"]'
			pass_sel = 'input[name="password"]'
			
			print("[FunPay] Заполняю логин...")
			login_input = await self._page.wait_for_selector(login_sel, timeout=10000)
			await login_input.click()
			await self._page.wait_for_timeout(300)
			await login_input.type(login, delay=100)
			
			print("[FunPay] Заполняю пароль...")
			pass_input = await self._page.wait_for_selector(pass_sel, timeout=10000)
			await pass_input.click()
			await self._page.wait_for_timeout(300)
			await pass_input.type(password, delay=100)
			
			print("[FunPay] ⏳ РЕШИТЕ КАПЧУ ВРУЧНУЮ В ОКНЕ БРАУЗЕРА И НАЖМИТЕ 'ВОЙТИ'")
			print("[FunPay] Ожидаю 5 минут (300 секунд) на решение капчи и вход...")
			
			# Ждём 5 минут (300 секунд), чтобы пользователь решил капчу и нажал кнопку
			for i in range(60):  # 60 * 5 = 300 секунд = 5 минут
				await self._page.wait_for_timeout(5000)
				html = await self._page.content()
				if "account/logout" in html or "Выйти" in html or "badge-balance" in html:
					print("[FunPay] ✅ Вход успешен!")
					await self._context.storage_state(path=config.storage_path)
					try:
						CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
						CREDENTIALS_PATH.write_text(json.dumps({"login": login, "password": password}), encoding="utf-8")
					except Exception:
						pass
					return True
				print(f"[FunPay] Проверка {i+1}/60... (осталось ~{300-(i+1)*5} сек)")
			
			print("[FunPay] ⏰ Время вышло. Проверяю финальный статус...")
			html = await self._page.content()
			if "account/logout" in html or "Выйти" in html:
				print("[FunPay] ✅ Вход успешен (финальная проверка)!")
				await self._context.storage_state(path=config.storage_path)
				return True
			else:
				print("[FunPay] ❌ Вход не удался — возможно, капча не решена или неверные данные")
				return False
		except Exception as e:
			print(f"[FunPay] Ошибка логина: {e}")
			import traceback
			traceback.print_exc()
			return False

	async def login_with_cookie_header(self, cookie_header: str) -> bool:
		"""Accepts raw Cookie header string, extracts known cookies and applies them to context."""
		if not self._browser:
			await self.launch()  # Создаём браузер если его нет
		
		from urllib.parse import unquote
		
		pairs = {}
		for part in cookie_header.split(';'):
			if '=' in part:
				name, value = part.split('=', 1)
				# Декодируем URL-encoded значения
				name = name.strip()
				value = unquote(value.strip())
				if name and value:  # Пропускаем пустые
					pairs[name] = value
		
		# Проверим, что есть хоть PHPSESSID или golden_key
		if not any(k in pairs for k in ["PHPSESSID", "golden_key"]):
			print("[FunPay] Не найдены обязательные куки (PHPSESSID или golden_key)")
			return False
		
		print(f"[FunPay] Найдено {len(pairs)} кук(и): {list(pairs.keys())}")
		
		# Добавим ВСЕ куки (не только выборочные)
		cookies = []
		for k, v in pairs.items():
			cookies.append({"name": k, "value": v, "domain": ".funpay.com", "path": "/"})
		
		try:
			print(f"[FunPay] Применяю {len(cookies)} кук(и) к домену .funpay.com...")
			for c in cookies:
				print(f"  - {c['name']}: {c['value'][:20]}...")
			await self._context.add_cookies(cookies)
			# Перезагружаем страницу с новыми куками
			if self._page:
				print("[FunPay] Перезагружаю страницу с новыми куками...")
				await self._page.goto(config.funpay_base_url, wait_until="domcontentloaded")
				await self._page.wait_for_timeout(3000)
				# Проверим, авторизованы ли мы
				html = await self._page.content()
				
				# Расширенная проверка авторизации
				checks = [
					"account/logout" in html,
					"Выйти" in html,
					"menu-item-logout" in html,
					'href="/account/settings"' in html,
					"badge-balance" in html
				]
				
				if any(checks):
					print(f"[FunPay] ✅ Авторизация успешна! (проверки: {sum(checks)}/5)")
					await self._context.storage_state(path=config.storage_path)
					return True
				else:
					print("[FunPay] ❌ Авторизация не прошла — куки не сработали")
					print(f"[FunPay] Подсказка: проверьте, не истекла ли сессия на FunPay")
					# Сохраним HTML для отладки
					with open("debug_login.html", "w", encoding="utf-8") as f:
						f.write(html)
					print("[FunPay] HTML страницы сохранён в debug_login.html")
					return False
			await self._context.storage_state(path=config.storage_path)
			print("[FunPay] Куки применены и сохранены")
			return True
		except Exception as e:
			print(f"[FunPay] Ошибка применения cookies: {e}")
			return False

	def load_saved_credentials(self) -> Optional[dict]:
		try:
			if CREDENTIALS_PATH.exists():
				data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
				# Совместимость: если есть email, переименуем в login
				if "email" in data and "login" not in data:
					data["login"] = data.pop("email")
				return data
			return None
		except Exception:
			return None

	async def _save_session(self) -> None:
		"""Сохранить текущую сессию"""
		if self._context:
			try:
				await self._context.storage_state(path=config.storage_path)
				print(f"[FunPay] Сессия сохранена в {config.storage_path}")
			except Exception as e:
				print(f"[FunPay] Ошибка сохранения сессии: {e}")

	def _load_processed_dialogs(self) -> None:
		"""Загрузить список обработанных диалогов"""
		try:
			if os.path.exists(self._processed_dialogs_file):
				with open(self._processed_dialogs_file, 'r', encoding='utf-8') as f:
					self._processed_dialogs = json.load(f)
				print(f"[FunPay] Загружено {len(self._processed_dialogs)} обработанных диалогов")
		except Exception as e:
			print(f"[FunPay] Ошибка загрузки обработанных диалогов: {e}")

	def _save_processed_dialogs(self) -> None:
		"""Сохранить список обработанных диалогов"""
		try:
			os.makedirs(os.path.dirname(self._processed_dialogs_file), exist_ok=True)
			with open(self._processed_dialogs_file, 'w', encoding='utf-8') as f:
				json.dump(self._processed_dialogs, f, ensure_ascii=False, indent=2)
			print(f"[FunPay] Сохранено {len(self._processed_dialogs)} обработанных диалогов")
		except Exception as e:
			print(f"[FunPay] Ошибка сохранения обработанных диалогов: {e}")

	async def fetch_balance(self) -> Optional[str]:
		# быстрый кэш на 10 секунд
		now = time.time()
		if self._cached_balance and now - self._cached_balance_ts < 10:
			return self._cached_balance
		if not self._page:
			return None
		selectors = [
			config.balance_selector,
			"[data-balance]",
			".header-balance",
			".balance",
		]
		urls = [
			config.funpay_base_url,
			config.funpay_base_url + "account/finance",
			config.funpay_base_url + "finance",
			config.funpay_base_url + "account",
		]
		try:
			for u in urls:
				await self._page.goto(u, wait_until="domcontentloaded")
				for sel in selectors:
					try:
						el = await self._page.query_selector(sel)
						if el:
							text = (await el.inner_text()).strip()
							if text:
								await self._save_session()  # Сохраняем сессию после успешного действия
								self._cached_balance = text
								self._cached_balance_ts = now
								return text
					except Exception:
						pass
				# Попытка парсить из текста страницы
				body_text = await self._page.inner_text("body")
				m = re.search(r"([\d\s]+(?:[.,]\d{2})?)\s*[₽RUB]", body_text, flags=re.IGNORECASE)
				if m:
					await self._save_session()  # Сохраняем сессию после успешного действия
					val = m.group(0).strip()
					self._cached_balance = val
					self._cached_balance_ts = now
					return val
			return None
		except Exception:
			return None

	async def fetch_trade_totals(self) -> Optional[dict]:
		"""Парсит страницу orders/trade и возвращает суммы по статусам.

		Возвращает словарь:
		{
		  'paid_sum': float, 'paid_count': int,
		  'closed_sum': float, 'closed_count': int,
		  'refund_sum': float, 'refund_count': int,
		  'total_sum': float
		}
		"""
		# быстрый кэш на 10 секунд
		now = time.time()
		if self._cached_trade_totals and now - self._cached_trade_totals_ts < 10:
			return self._cached_trade_totals
		try:
			if not self._browser:
				await self.launch()
			assert self._context is not None
			page = await self._ensure_orders_page()
			url = config.funpay_base_url + "orders/trade?state=paid"
			if page.url != url:
				await page.goto(url, wait_until="domcontentloaded")
			# Подождём таблицу, но не падаем, если не нашли
			try:
				await page.wait_for_selector("table", timeout=3000)
			except Exception:
				pass

			rows = await page.query_selector_all("tr")
			paid_sum = closed_sum = refund_sum = 0.0
			paid_count = closed_count = refund_count = 0

			async def extract_sum(text: str) -> Optional[float]:
				m = re.search(r"([\d\s]+(?:[.,]\d{2})?)\s*[₽RrРр]", text)
				if not m:
					return None
				val = m.group(1).replace(" ", "").replace(",", ".")
				try:
					return float(val)
				except Exception:
					return None

			if rows:
				for r in rows:
					try:
						text = (await r.inner_text()).strip()
						if not text or ("Дата" in text and "Сумма" in text):
							continue  # пропустим заголовки
						amount = await extract_sum(text)
						if amount is None:
							continue
						if "Оплачен" in text:
							paid_sum += amount
							paid_count += 1
						elif "Закрыт" in text:
							closed_sum += amount
							closed_count += 1
						elif "Возврат" in text:
							refund_sum += amount
							refund_count += 1
					except Exception:
						continue
			else:
				# Фоллбек: парсим весь текст страницы
				body_text = await page.inner_text("body")
				for line in body_text.splitlines():
					amount = await extract_sum(line)
					if amount is None:
						continue
					if "Оплачен" in line:
						paid_sum += amount; paid_count += 1
					elif "Закрыт" in line:
						closed_sum += amount; closed_count += 1
					elif "Возврат" in line:
						refund_sum += amount; refund_count += 1

			result = {
				"paid_sum": round(paid_sum, 2),
				"paid_count": paid_count,
				"closed_sum": round(closed_sum, 2),
				"closed_count": closed_count,
				"refund_sum": round(refund_sum, 2),
				"refund_count": refund_count,
				"total_sum": round(paid_sum + closed_sum + refund_sum, 2),
			}
			await self._save_session()
			self._cached_trade_totals = result
			self._cached_trade_totals_ts = now
			return result
		except Exception as e:
			print(f"[FunPay] Ошибка парсинга orders/trade: {e}")
			try:
				await self._page.screenshot(path="debug_orders_trade.png")
			except Exception:
				pass
			return None

	async def fetch_active_orders(self, limit: int = 10) -> Optional[list]:
		"""Возвращает список активных заказов (статус 'Оплачен').
		Список элементов: { 'order_id', 'buyer', 'status', 'amount', 'description', 'date' }
		"""
		# быстрый кэш на 10 секунд
		now = time.time()
		if self._cached_active_orders and now - self._cached_active_orders_ts < 10:
			return self._cached_active_orders[:limit]
		try:
			if not self._browser:
				await self.launch()
			assert self._context is not None
			page = await self._ensure_orders_page()
			url = config.funpay_base_url + "orders/trade?state=paid"
			if page.url != url:
				await page.goto(url, wait_until="domcontentloaded")

			# Попробуем новый макет списка заказов (.tc-item)
			def clean_text(s: str) -> str:
				return re.sub(r"\s+", " ", s).strip()

			orders = []
			items = await page.query_selector_all(".tc-item")
			if items:
				for it in items:
					try:
						# Статус
						status_el = await it.query_selector(".tc-status")
						status_txt = clean_text(await status_el.inner_text()) if status_el else ""
						status_norm = status_txt.lower()
						# Строго берём только 'оплачен', исключая закрытые и возвраты
						is_paid = ("оплачен" in status_norm) and ("закрыт" not in status_norm) and ("возврат" not in status_norm)
						if not is_paid:
							continue

						# Идентификатор заказа
						order_el = await it.query_selector(".tc-order, .tc-order a")
						order_txt = clean_text(await order_el.inner_text()) if order_el else ""
						m_id = re.search(r"#\s*([A-Za-z0-9-]+)", order_txt)
						order_id = ("#" + m_id.group(1)) if m_id else order_txt or None

						# Покупатель
						buyer_el = await it.query_selector(".tc-buyer, .tc-buyer .media-user-name, .media-user-name")
						buyer_txt = clean_text(await buyer_el.inner_text()) if buyer_el else ""

						# Сумма
						amount_el = await it.query_selector(".tc-sum, .tc-amount, .tc-price, .tc-total")
						amount_txt = clean_text(await amount_el.inner_text()) if amount_el else ""
						m_sum = re.search(r"([\d\s]+(?:[.,]\d{2})?)\s*[₽RrРр]|([\d\s]+(?:[.,]\d{2})?)", amount_txt)
						amount_val = None
						if m_sum:
							grp = m_sum.group(1) or m_sum.group(2)
							amount_val = grp.replace(" ", "").replace(",", ".")

						# Описание/дата (необязательно)
						desc_el = await it.query_selector(".tc-desc, .tc-title, .tc-game")
						desc_txt = clean_text(await desc_el.inner_text()) if desc_el else ""
						date_el = await it.query_selector(".tc-date, time, .tc-time")
						date_txt = clean_text(await date_el.inner_text()) if date_el else ""

						orders.append({
							"date": date_txt,
							"order_id": order_id or "",
							"description": desc_txt,
							"buyer": buyer_txt,
							"status": status_txt or "Оплачен",
							"amount": amount_val or amount_txt,
						})
					except Exception:
						continue
			else:
				# Фоллбек на табличный макет
				try:
					await page.wait_for_selector("tbody tr, tr", timeout=3000)
				except Exception:
					pass
				rows = await page.query_selector_all("tbody tr")
				if not rows:
					rows = await page.query_selector_all("tr")
				for r in rows:
					try:
						cells = await r.query_selector_all("td")
						if cells and len(cells) >= 6:
							date_txt = clean_text(await cells[0].inner_text())
							order_txt = clean_text(await cells[1].inner_text())
							desc_txt = clean_text(await cells[2].inner_text())
							buyer_txt = clean_text(await cells[3].inner_text())
							status_txt = clean_text(await cells[4].inner_text())
							amount_txt = clean_text(await cells[5].inner_text())
						else:
							row_text = clean_text(await r.inner_text())
							date_txt = ""
							order_txt = row_text
							desc_txt = row_text
							buyer_txt = row_text
							status_txt = row_text
							amount_txt = row_text

						m_id = re.search(r"#([A-Z0-9]{6,})", order_txt)
						order_id = m_id.group(0) if m_id else None

						m_sum = re.search(r"([\d\s]+(?:[.,]\d{2})?)\s*[₽RrРр]", amount_txt)
						amount = m_sum.group(1).replace(" ", "").replace(",", ".") if m_sum else None

						status_norm = status_txt.lower()
						if status_txt and ("оплачен" in status_norm) and ("закрыт" not in status_norm) and ("возврат" not in status_norm):
							orders.append({
								"date": date_txt,
								"order_id": order_id or order_txt,
								"description": desc_txt,
								"buyer": buyer_txt,
								"status": status_txt,
								"amount": amount or amount_txt,
							})
					except Exception:
						continue

			# Ограничим и сохраним сессию
			orders = orders[:limit]
			await self._save_session()

			# Если ничего не нашли — сохраним HTML/скрин для отладки
			if not orders:
				try:
					await page.screenshot(path="debug_orders_trade.png", full_page=False)
					html = await page.content()
					with open("debug_orders_trade.html", "w", encoding="utf-8") as f:
						f.write(html)
					print("[FunPay] Активные заказы не найдены — сохранены debug_orders_trade.png/html")
				except Exception:
					pass

			self._cached_active_orders = orders
			self._cached_active_orders_ts = now
			return orders
		except Exception as e:
			print(f"[FunPay] Ошибка получения активных заказов: {e}")
			return None

	async def get_unread_dialogs(self) -> list:
		"""Получить список непрочитанных диалогов с именами и ID"""
		if not self._browser or not self._page or self._page.is_closed():
			await self.launch()
		try:
			print("[FunPay] Переход на /chat/...")
			await self._page.goto("https://funpay.com/chat/", wait_until="networkidle")
			print("[FunPay] Ожидаем появления диалогов...")
			
			# Ждём появления контейнера со списком
			try:
				await self._page.wait_for_selector(".contact-list", timeout=5000)
				print("[FunPay] Контейнер .contact-list найден")
			except Exception:
				print("[FunPay] Контейнер .contact-list не найден за 5 сек")
			
			await self._page.wait_for_timeout(2000)
			
			# Получим HTML для отладки
			html = await self._page.content()
			if ".contact-item" in html:
				print("[FunPay] В HTML найден .contact-item")
			else:
				print("[FunPay] В HTML НЕТ .contact-item — возможно, нужна авторизация")
			
			# Попробуем несколько селекторов
			selectors = [
				"a.contact-item",
				".contact-item",
				".contact-list a",
				"a[data-id]"
			]
			dialogs = None
			for sel in selectors:
				dialogs = await self._page.query_selector_all(sel)
				if dialogs:
					print(f"[FunPay] Найдено {len(dialogs)} элементов с селектором '{sel}'")
					break
			
			if not dialogs:
				print("[FunPay] Диалоги не найдены. Сохраню скриншот и HTML...")
				await self._page.screenshot(path="debug_chat.png")
				with open("debug_chat.html", "w", encoding="utf-8") as f:
					f.write(html)
				print("[FunPay] Скриншот: debug_chat.png, HTML: debug_chat.html")
				return []
			
			result = []
			for i, dialog in enumerate(dialogs[:20]):
				try:
					name_el = await dialog.query_selector(".media-user-name")
					name = (await name_el.inner_text()).strip() if name_el else f"Диалог {i+1}"
					node_id = await dialog.get_attribute("data-id")
					unread_class = await dialog.get_attribute("class")
					is_unread = "unread" in (unread_class or "")
					print(f"[FunPay] Диалог #{i+1}: {name} (node_id={node_id}, unread={is_unread})")
					result.append({"name": name, "node_id": node_id, "unread": is_unread})
				except Exception as e:
					print(f"[FunPay] Ошибка парсинга диалога #{i+1}: {e}")
					continue
			print(f"[FunPay] Итого найдено {len(result)} диалогов")
			await self._save_session()  # Сохраняем сессию после успешного действия
			return result
		except Exception as e:
			print(f"[FunPay] Ошибка get_unread_dialogs: {e}")
			import traceback
			traceback.print_exc()
			return []

	async def reply_to_dialog(self, node_id: str, text: str) -> bool:
		"""Отправить сообщение в конкретный диалог по node_id"""
		if not self._browser:
			await self.launch()
		try:
			print(f"[FunPay] Открываю диалог {node_id}...")
			await self._page.goto(f"https://funpay.com/chat/?node={node_id}", wait_until="networkidle")
			
			# Ждём появления формы чата
			try:
				await self._page.wait_for_selector(".chat-form", timeout=5000)
				print("[FunPay] Форма чата загружена")
			except Exception:
				print("[FunPay] Форма чата не найдена")
			
			await self._page.wait_for_timeout(1500)
			
			# Попробуем разные селекторы с использованием locator
			selectors = [
				"textarea[name='content']",
				".chat-form textarea",
				"form[action*='message'] textarea",
				"textarea"
			]
			
			reply_locator = None
			for sel in selectors:
				try:
					loc = self._page.locator(sel).first
					await loc.wait_for(state="visible", timeout=2000)
					reply_locator = loc
					print(f"[FunPay] Найден инпут: {sel}")
					break
				except Exception:
					continue
			
			if not reply_locator:
				print("[FunPay] Инпут ответа не найден ни одним селектором")
				return False
			
			await reply_locator.fill(text)
			print(f"[FunPay] Текст заполнен: {text[:30]}...")
			
			# Попробуем найти кнопку отправки
			send_selectors = [
				".chat-form button[type='submit']",
				"button:has-text('Отправить')",
				".chat-form-btn button"
			]
			
			sent = False
			for sel in send_selectors:
				try:
					btn = self._page.locator(sel).first
					await btn.click(timeout=2000)
					print(f"[FunPay] Кликнул кнопку: {sel}")
					sent = True
					break
				except Exception:
					continue
			
			if not sent:
				# Если кнопку не нашли, нажмём Enter
				await reply_locator.press("Enter")
				print("[FunPay] Нажал Enter")
			
			print(f"[FunPay] ✅ Отправлено в диалог {node_id}")
			await self._save_session()  # Сохраняем сессию после успешного действия
			return True
		except Exception as e:
			print(f"[FunPay] Ошибка reply_to_dialog: {e}")
			import traceback
			traceback.print_exc()
			return False

	async def reply_first_unread(self, text: str) -> bool:
		if not self._page:
			print("[FunPay] reply_first_unread: нет страницы")
			return False
		try:
			print("[FunPay] Переход на /chat/...")
			await self._page.goto(config.funpay_base_url + "chat/", wait_until="domcontentloaded")
			print("[FunPay] Ищу непрочитанный диалог...")
			unread = await self._page.query_selector(config.unread_dialog_selector)
			if not unread:
				print("[FunPay] Непрочитанных нет, беру первый диалог...")
				unread = await self._page.query_selector(".contact-list a.contact-item")
				if not unread:
					print("[FunPay] Не нашёл вообще ни одного диалога")
					return False
			print(f"[FunPay] Нашёл диалог, кликаю...")
			await unread.click()
			await self._page.wait_for_load_state("domcontentloaded")
			print("[FunPay] Ищу поле ввода...")
			editor_selectors = [
				"textarea[name='content']",
				config.dialog_reply_input_selector,
				"textarea#message",
				"textarea",
				"div[role='textbox']",
				"div[contenteditable=true]",
			]
			reply = None
			for es in editor_selectors:
				try:
					reply = await self._page.wait_for_selector(es, timeout=2000)
					if reply:
						print(f"[FunPay] Нашёл поле: {es}")
						break
				except Exception:
					pass
			if not reply:
				print("[FunPay] Не нашёл поле ввода, пробую fallback...")
				locator = await self._find_chat_input()
				if not locator:
					print("[FunPay] Fallback тоже не нашёл инпут")
					return False
				await locator.fill(text)
				send = await self._page.query_selector(config.dialog_reply_send_selector)
				if send:
					await send.click()
				else:
					await locator.press("Enter")
				print("[FunPay] Отправлено через fallback")
				return True
			await reply.fill(text)
			print(f"[FunPay] Заполнил текст: {text[:30] if len(text) > 30 else text}...")
			send = await self._page.query_selector(".chat-form-btn button[type='submit']")
			if not send:
				send = await self._page.query_selector(config.dialog_reply_send_selector)
			if send:
				await send.click()
				print("[FunPay] Кликнул отправку")
			else:
				await reply.press("Enter")
				print("[FunPay] Нажал Enter")
			return True
		except Exception as e:
			print(f"[FunPay] Ошибка reply_first_unread: {e}")
			return False

	async def _find_chat_input(self) -> Optional[Page]:
		assert self._page is not None
		candidates = [
			config.chat_input_selector,
			"textarea",
			"div[contenteditable=true]",
		]
		for sel in candidates:
			try:
				locator = self._page.locator(sel).first
				await locator.wait_for(state="visible", timeout=3000)
				return locator
			except Exception:
				continue
		return None

	async def _send_to_chat_once(self) -> None:
		# Используем готовую вкладку для услуг
		page = self._services_page or self._page
		if not page:
			print("[FunPay] Нет вкладки для услуг")
			return
		try:
			# Если вкладка не на нужной странице, переходим
			if not page.url.startswith(config.funpay_section_url):
				await page.goto(config.funpay_section_url, wait_until="domcontentloaded")
			
			# Получаем ID текущей услуги для отслеживания
			service_id = None
			try:
				# Пробуем получить ID из URL или других элементов
				current_url = page.url
				if "offer" in current_url:
					service_id = current_url.split("offer/")[-1].split("?")[0]
				else:
					# Ищем ID в элементах страницы
					service_element = await page.query_selector("[data-offer-id], [data-id], .offer-item")
					if service_element:
						service_id = await service_element.get_attribute("data-offer-id") or await service_element.get_attribute("data-id")
			except Exception:
				pass
			
			# Для услуг НЕ проверяем обработанные
			# В услугах нужно писать постоянно
			
			# Сначала проверяем, не открыт ли уже чат
			# ТОЛЬКО правильные поля для чата
			chat_input_selectors = [
				"textarea[name='content']",
				"textarea",
				".chat-form textarea",
				".chat-input textarea",
				"#message",
				"[placeholder*='сообщение']",
				"[placeholder*='message']"
			]
			
			# Проверяем, есть ли уже поле ввода (чат уже открыт)
			existing_input = None
			for selector in chat_input_selectors:
				try:
					element = await page.query_selector(selector)
					if element:
						# Проверяем, что элемент видим
						is_visible = await element.is_visible()
						if is_visible:
							existing_input = element
							print(f"[FunPay] Чат уже открыт, найдено видимое поле: {selector}")
							break
						else:
							print(f"[FunPay] Поле найдено, но скрыто: {selector}")
				except Exception:
					continue
			
			# Если чат не открыт, ищем кнопку "Открыть чат"
			if not existing_input:
				chat_button_selectors = [
					"button:has-text('Открыть чат')",
					"button:has-text('Open chat')",
					".btn:has-text('Открыть чат')",
					"a:has-text('Открыть чат')",
					"[data-action='open-chat']"
				]
				
				chat_opened = False
				for selector in chat_button_selectors:
					try:
						chat_btn = await page.query_selector(selector)
						if chat_btn:
							print(f"[FunPay] Найдена кнопка чата: {selector}")
							await chat_btn.click()
							await page.wait_for_timeout(2000)  # Ждём открытия чата
							
							# Ждём появления видимого поля ввода
							await page.wait_for_timeout(2000)
							for input_selector in chat_input_selectors:
								try:
									element = await page.query_selector(input_selector)
									if element and await element.is_visible():
										existing_input = element
										print(f"[FunPay] Чат открыт, найдено видимое поле: {input_selector}")
										chat_opened = True
										break
								except Exception:
									continue
							
							# Если не нашли правильное поле, ждём ещё
							if not chat_opened:
								await page.wait_for_timeout(2000)
								for input_selector in chat_input_selectors:
									try:
										element = await page.query_selector(input_selector)
										if element and await element.is_visible():
											existing_input = element
											print(f"[FunPay] Чат открыт (повторный поиск), найдено видимое поле: {input_selector}")
											chat_opened = True
											break
									except Exception:
										continue
							if chat_opened:
								break
					except Exception:
						continue
				
				if not chat_opened:
					print("[FunPay] Кнопка 'Открыть чат' не найдена — пропускаю отправку")
					return
			
			# Используем уже найденное поле или ищем заново
			locator = existing_input
			if not locator:
				# Если поле не найдено, ждём появления правильного поля
				print("[FunPay] Жду появления правильного поля ввода...")
				try:
					# Ждём появления textarea с таймаутом
					locator = await page.wait_for_selector("textarea[name='content']", timeout=5000)
					if locator and await locator.is_visible():
						print("[FunPay] Найдено правильное поле: textarea[name='content']")
					else:
						locator = None
				except Exception:
					pass
				
				# Если не нашли, пробуем другие селекторы
				if not locator:
					for selector in chat_input_selectors:
						try:
							element = await page.query_selector(selector)
							if element and await element.is_visible():
								locator = element
								print(f"[FunPay] Найдено видимое поле ввода: {selector}")
								break
						except Exception:
							continue
			
			if not locator:
				print("[FunPay] Правильное поле чата не найдено — пропускаю отправку")
				# Сохраним скриншот для отладки
				try:
					await page.screenshot(path="debug_chat_input.png")
					print("[FunPay] Скриншот сохранён: debug_chat_input.png")
				except Exception:
					pass
				return
			
			# Дополнительная проверка - убеждаемся, что это правильное поле
			tag_name = await locator.evaluate("el => el.tagName")
			if tag_name.lower() != 'textarea':
				print(f"[FunPay] Найдено неправильное поле: {tag_name}, пропускаю")
				return
			
			# Очищаем поле и вводим текст правильно
			try:
				# Принудительно ждём видимости элемента
				await locator.wait_for_element_state("visible", timeout=5000)
				await locator.click()
				await page.wait_for_timeout(500)
				await locator.fill("")  # Полная очистка
				await page.wait_for_timeout(300)
			except Exception as e:
				print(f"[FunPay] Ошибка при клике на поле: {e}")
				return
			
			# Проверяем, что элемент всё ещё доступен
			try:
				await locator.is_visible()
			except Exception:
				print("[FunPay] Поле ввода стало недоступным")
				return
			
			# Вводим текст по частям для избежания искажений
			try:
				text_parts = self._post_text.split()
				for i, part in enumerate(text_parts):
					await locator.type(part, delay=50)
					if i < len(text_parts) - 1:
						await locator.type(" ", delay=20)
					await page.wait_for_timeout(100)
			except Exception as e:
				print(f"[FunPay] Ошибка при вводе по частям, пробую fill: {e}")
				try:
					# Альтернативный способ - сразу весь текст
					await locator.fill(self._post_text)
				except Exception as e2:
					print(f"[FunPay] Ошибка при fill: {e2}")
					return
			
			print(f"[FunPay] Текст введён в поле: {self._post_text[:50]}...")
			
			# Всегда нажимаем Enter для отправки (как в обычном чате)
			try:
				await page.wait_for_timeout(500)
				await locator.press("Enter")
				print("[FunPay] Нажат Enter для отправки")
				
				# Ждём немного и проверяем, что сообщение отправилось
				await page.wait_for_timeout(2000)
				print(f"[FunPay] ✅ Отправлено в чат: {self._post_text[:50]}...")
			except Exception as e:
				print(f"[FunPay] Ошибка при отправке: {e}")
				return
			
			# Для услуг НЕ отмечаем как обработанные
			# В услугах нужно писать постоянно
		except Exception as e:
			print(f"[FunPay] Ошибка отправки: {e}")

	async def send_message_with_screenshot(self) -> list:
		"""Отправить сообщение с сохранением скриншотов процесса"""
		if not self._page:
			await self.launch()
		
		screenshots = []
		try:
			await self._page.goto(config.funpay_section_url, wait_until="domcontentloaded")
			
			# Скриншот 1: Страница до открытия чата
			path1 = "debug_send_1_before_chat.png"
			await self._page.screenshot(path=path1)
			screenshots.append(path1)
			print("[FunPay] Скриншот 1: Страница до открытия чата")
			
			# Ищем и нажимаем кнопку "Открыть чат"
			chat_button_selectors = [
				"button:has-text('Открыть чат')",
				"button:has-text('Open chat')",
				".btn:has-text('Открыть чат')",
				"a:has-text('Открыть чат')",
				"[data-action='open-chat']"
			]
			
			chat_opened = False
			for selector in chat_button_selectors:
				try:
					chat_btn = await self._page.query_selector(selector)
					if chat_btn:
						print(f"[FunPay] Найдена кнопка чата: {selector}")
						await chat_btn.click()
						await self._page.wait_for_timeout(2000)
						chat_opened = True
						break
				except Exception:
					continue
			
			if not chat_opened:
				print("[FunPay] Кнопка 'Открыть чат' не найдена")
				return screenshots
			
			# Скриншот 2: После открытия чата
			path2 = "debug_send_2_chat_opened.png"
			await self._page.screenshot(path=path2)
			screenshots.append(path2)
			print("[FunPay] Скриншот 2: Чат открыт")
			
			# Ищем поле ввода
			chat_input_selectors = [
				"textarea[name='content']",
				"textarea",
				"input[type='text']",
				".chat-form textarea",
				".chat-input textarea",
				"#message",
				"[placeholder*='сообщение']",
				"[placeholder*='message']"
			]
			
			locator = None
			for selector in chat_input_selectors:
				try:
					locator = await self._page.query_selector(selector)
					if locator:
						print(f"[FunPay] Найдено поле ввода: {selector}")
						break
				except Exception:
					continue
			
			if not locator:
				print("[FunPay] Поле ввода не найдено")
				path3 = "debug_send_3_no_input.png"
				await self._page.screenshot(path=path3)
				screenshots.append(path3)
				return screenshots
			
			# Скриншот 3: Поле ввода найдено
			path3 = "debug_send_3_input_found.png"
			await self._page.screenshot(path=path3)
			screenshots.append(path3)
			print("[FunPay] Скриншот 3: Поле ввода найдено")
			
			# Вводим тестовое сообщение правильно
			test_message = "TEST BOTA - " + self._post_text[:50]
			await locator.click()
			await self._page.wait_for_timeout(500)
			await locator.fill("")
			await self._page.wait_for_timeout(300)
			
			# Вводим текст по частям
			text_parts = test_message.split()
			for i, part in enumerate(text_parts):
				await locator.type(part, delay=50)
				if i < len(text_parts) - 1:
					await locator.type(" ", delay=20)
				await self._page.wait_for_timeout(100)
			
			# Скриншот 4: Текст введён
			path4 = "debug_send_4_text_entered.png"
			await self._page.screenshot(path=path4)
			screenshots.append(path4)
			print("[FunPay] Скриншот 4: Текст введён")
			
			# Нажимаем Enter для отправки
			await self._page.wait_for_timeout(500)
			await locator.press("Enter")
			print("[FunPay] Нажат Enter для отправки")
			
			# Скриншот 5: После отправки
			await self._page.wait_for_timeout(2000)
			path5 = "debug_send_5_after_send.png"
			await self._page.screenshot(path=path5)
			screenshots.append(path5)
			print("[FunPay] Скриншот 5: После отправки")
			
			return screenshots
		except Exception as e:
			print(f"[FunPay] Ошибка создания скриншота: {e}")
			return screenshots

	async def get_chat_screenshots(self) -> list:
		"""Получить скриншоты личных чатов"""
		if not self._page:
			await self.launch()
		
		screenshots = []
		try:
			# Переходим на страницу чатов
			await self._page.goto("https://funpay.com/chat/", wait_until="domcontentloaded")
			await self._page.wait_for_timeout(2000)
			
			# Скриншот 1: Общий вид чатов
			path1 = "debug_chat_1_overview.png"
			await self._page.screenshot(path=path1)
			screenshots.append(path1)
			print("[FunPay] Скриншот 1: Общий вид чатов")
			
			# Получаем список диалогов
			dialogs = await self.get_unread_dialogs()
			if not dialogs:
				print("[FunPay] Диалоги не найдены")
				return screenshots
			
			# Скриншоты первых 3 диалогов
			for i, dialog in enumerate(dialogs[:3]):
				try:
					# Кликаем на диалог
					dialog_selector = f"a[data-id='{dialog['node_id']}']"
					dialog_elem = await self._page.query_selector(dialog_selector)
					if dialog_elem:
						await dialog_elem.click()
						await self._page.wait_for_timeout(1500)
						
						# Скриншот диалога
						path = f"debug_chat_{i+2}_{dialog['name'].replace(' ', '_')}.png"
						await self._page.screenshot(path=path)
						screenshots.append(path)
						print(f"[FunPay] Скриншот {i+2}: Диалог с {dialog['name']}")
						
						# Возвращаемся к списку диалогов
						await self._page.goto("https://funpay.com/chat/", wait_until="domcontentloaded")
						await self._page.wait_for_timeout(1000)
				except Exception as e:
					print(f"[FunPay] Ошибка скриншота диалога {i+1}: {e}")
					continue
			
			return screenshots
		except Exception as e:
			print(f"[FunPay] Ошибка создания скриншотов чатов: {e}")
			return screenshots

	async def test_auto_reply(self) -> bool:
		"""Тест автоответа - отправить сообщение в первый доступный диалог"""
		chat_page = self._chat_page or self._page
		if not chat_page:
			print("[FunPay] Нет вкладки для чатов")
			return False
		
		try:
			# Переходим на страницу чатов
			await chat_page.goto("https://funpay.com/chat/", wait_until="domcontentloaded")
			await chat_page.wait_for_timeout(2000)
			
			# Получаем список диалогов
			dialogs = await self.get_unread_dialogs()
			if not dialogs:
				print("[FunPay] Нет диалогов для тестирования")
				return False
			
			# Берём первый диалог
			dialog = dialogs[0]
			print(f"[FunPay] Тестирую автоответ в диалоге: {dialog['name']}")
			
			# Кликаем на диалог
			dialog_selector = f"a[data-id='{dialog['node_id']}']"
			dialog_elem = await chat_page.query_selector(dialog_selector)
			if not dialog_elem:
				print("[FunPay] Не удалось найти диалог")
				return False
			
			await dialog_elem.click()
			await chat_page.wait_for_load_state("domcontentloaded")
			await chat_page.wait_for_timeout(1500)
			
			# Ищем поле ввода
			editor_selectors = [
				"textarea[name='content']",
				config.dialog_reply_input_selector,
				"textarea#message",
				"textarea",
			]
			
			reply_elem = None
			for sel in editor_selectors:
				try:
					reply_elem = await chat_page.wait_for_selector(sel, timeout=2000)
					if reply_elem:
						print(f"[FunPay] Найдено поле ввода: {sel}")
						break
				except Exception:
					continue
			
			if not reply_elem:
				print("[FunPay] Поле ввода не найдено")
				return False
			
			# Отправляем тестовое сообщение
			test_message = f"TEST AUTO REPLY - {config.auto_reply_text}"
			await reply_elem.fill(test_message)
			await reply_elem.press("Enter")
			
			print(f"[FunPay] ✅ Тестовый автоответ отправлен: {test_message[:50]}...")
			return True
			
		except Exception as e:
			print(f"[FunPay] Ошибка тестирования автоответа: {e}")
			return False

	async def _periodic_poster(self) -> None:
		while self._running:
			await self._send_to_chat_once()
			await asyncio.sleep(self._post_interval_sec)

	async def _check_chats_during_wait(self, wait_time: int) -> None:
		"""Проверяет чаты во время ожидания между отправками в услуги"""
		if not config.auto_reply_enabled:
			await asyncio.sleep(wait_time)
			return
		
		chat_page = self._chat_page or self._page
		if not chat_page:
			await asyncio.sleep(wait_time)
			return
		
		# Разбиваем время ожидания на маленькие интервалы для проверки чатов
		check_interval = 2  # Проверяем каждые 2 секунды
		remaining_time = wait_time
		
		while remaining_time > 0 and self._running:
			try:
				# Переходим на страницу чатов
				if not chat_page.url.startswith("https://funpay.com/chat"):
					await chat_page.goto("https://funpay.com/chat/", wait_until="domcontentloaded")
					await asyncio.sleep(0.3)
				
				# Ищем диалоги с новыми сообщениями
				unread_selectors = [
					config.unread_dialog_selector,
					".contact-list a.contact-item.unread",
					".contact-item.unread",
					"a.contact-item.unread",
					".unread",
					"[class*='unread']"
				]
				
				dialog = None
				for selector in unread_selectors:
					try:
						dialog = await chat_page.query_selector(selector)
						if dialog:
							print(f"[FunPay] Найден диалог с новым сообщением во время ожидания: {selector}")
							break
					except Exception:
						continue
				
				if dialog:
					# Получаем ID диалога
					dialog_id = None
					try:
						dialog_id = await dialog.get_attribute("data-id")
						if not dialog_id:
							dialog_id = await dialog.get_attribute("href")
						if not dialog_id:
							dialog_id = str(hash(await dialog.get_attribute("href") or "unknown"))
					except Exception:
						dialog_id = "unknown"
					
					# Проверяем, не отвечали ли мы недавно
					current_time = time.time()
					should_reply = True
					if dialog_id in self._processed_dialogs:
						last_reply_time = self._processed_dialogs[dialog_id]
						if isinstance(last_reply_time, (int, float)):
							time_since_last = current_time - last_reply_time
							if time_since_last < 120:  # 2 минуты
								should_reply = False
								print(f"[FunPay] >> Диалог {dialog_id} обработан недавно, пропускаю")
					
					if should_reply:
						print("[FunPay] >> Открываю диалог для автоответа во время ожидания!")
						
						# Кликаем на диалог
						await dialog.click()
						await chat_page.wait_for_load_state("domcontentloaded")
						await asyncio.sleep(0.8)
						
						# Ищем поле ввода
						editor_selectors = [
							"textarea[name='content']",
							config.dialog_reply_input_selector,
							"textarea#message",
							"textarea",
						]
						
						reply_elem = None
						for sel in editor_selectors:
							try:
								reply_elem = await chat_page.wait_for_selector(sel, timeout=1500)
								if reply_elem:
									break
							except Exception:
								continue
						
						if reply_elem:
							# Заполняем текст
							await reply_elem.fill(config.auto_reply_text)
							print(f"[FunPay] >> Отправляю автоответ: {config.auto_reply_text[:40]}...")
							
							# Нажимаем Enter для отправки
							await reply_elem.press("Enter")
							
							await self._save_session()
							print("[FunPay] >> Автоответ отправлен во время ожидания!")
							
							# Отмечаем диалог как обработанный
							self._processed_dialogs[dialog_id] = current_time
							self._save_processed_dialogs()
							print(f"[FunPay] >> Диалог {dialog_id} отмечен как обработанный")
						else:
							print("[FunPay] >> Не нашёл поле ввода")
						
						# Возвращаемся к услугам
						await asyncio.sleep(1)
				
				# Ждём до следующей проверки
				sleep_time = min(check_interval, remaining_time)
				await asyncio.sleep(sleep_time)
				remaining_time -= sleep_time
				
			except Exception as e:
				print(f"[FunPay] Ошибка проверки чатов во время ожидания: {e}")
				await asyncio.sleep(check_interval)
				remaining_time -= check_interval

	async def _services_auto_post_loop(self) -> None:
		"""Постоянная отправка текста в услуги с заданным интервалом"""
		if not self._post_text:
			print("[FunPay] Текст для услуг не задан")
			return
		
		# Используем готовую вкладку для услуг
		services_page = self._services_page or self._page
		if not services_page:
			print("[FunPay] Нет вкладки для услуг")
			return
		
		interval = getattr(config, 'services_interval', 5)  # По умолчанию 5 секунд
		print(f"[FunPay] Запущена постоянная отправка в услуги (интервал: {interval} сек)")
		print(f"[FunPay] Текст: {self._post_text[:50]}...")
		
		while self._running:
			try:
				await self._send_to_chat_once()
				print(f"[FunPay] Жду {interval} секунд до следующей отправки...")
				
				# Во время ожидания проверяем чаты на новые сообщения
				await self._check_chats_during_wait(interval)
				
			except Exception as e:
				print(f"[FunPay] Ошибка в цикле услуг: {e}")
				await asyncio.sleep(5)  # Ждём 5 секунд при ошибке

	async def _auto_reply_and_screenshot_loop(self) -> None:
		"""Объединённый процесс: проверяет новые сообщения, делает скриншот и отправляет автоответ"""
		if not config.auto_reply_enabled:
			print("[FunPay] Автоответ отключён в конфиге (auto_reply_enabled=False)")
			return
		
		# Используем готовую вкладку для чатов
		chat_page = self._chat_page or self._page
		if not chat_page:
			print("[FunPay] Нет вкладки для чатов, жду 3 секунды...")
			await asyncio.sleep(3)
			if not self._page:
				print("[FunPay] Страница так и не загрузилась, автоответ не запущен")
				return
			chat_page = self._page
		
		print("[FunPay] Запущен мониторинг сообщений (проверка каждые ~2 сек)")
		print(f"[FunPay] Ищу диалоги с новыми сообщениями")
		
		while self._running:
			try:
				# Обновляем страницу чатов если нужно
				if not chat_page.url.startswith("https://funpay.com/chat"):
					await chat_page.goto("https://funpay.com/chat/", wait_until="domcontentloaded")
					await asyncio.sleep(0.3)
				
				# Ищем ВСЕ диалоги (и непрочитанные, и обычные)
				all_dialog_selectors = [
					config.unread_dialog_selector,
					".contact-list a.contact-item.unread",
					".contact-item.unread", 
					"a.contact-item.unread",
					".unread",
					"[class*='unread']",
					".contact-list a.contact-item",
					".contact-item",
					"a.contact-item"
				]
				
				dialog = None
				for selector in all_dialog_selectors:
					try:
						dialog = await chat_page.query_selector(selector)
						if dialog:
							print(f"[FunPay] Найден диалог: {selector}")
							break
					except Exception:
						continue
				
				if dialog:
					# Получаем ID диалога для защиты от спама
					dialog_id = None
					try:
						dialog_id = await dialog.get_attribute("data-id")
						if not dialog_id:
							dialog_id = await dialog.get_attribute("href")
						if not dialog_id:
							dialog_id = str(hash(await dialog.get_attribute("href") or "unknown"))
					except Exception:
						dialog_id = "unknown"
					
					# Проверяем, не отвечали ли мы недавно в этот диалог
					current_time = time.time()
					if dialog_id in self._processed_dialogs:
						last_reply_time = self._processed_dialogs[dialog_id]
						if isinstance(last_reply_time, (int, float)):
							time_since_last = current_time - last_reply_time
							# Если прошло меньше 2 минут, пропускаем
							if time_since_last < 120:  # 2 минуты
								print(f"[FunPay] >> Диалог {dialog_id} обработан недавно ({time_since_last:.1f}с назад), пропускаю")
								await asyncio.sleep(2)
								continue
					
					# Проверяем, не открыт ли уже этот диалог
					current_url = chat_page.url
					if dialog_id in current_url or "chat" in current_url:
						print(f"[FunPay] >> Диалог {dialog_id} уже открыт, пропускаю")
						await asyncio.sleep(5)
						continue
					
					print("[FunPay] >> Открываю диалог для автоответа!")
					
					# Кликаем на диалог
					await dialog.click()
					await chat_page.wait_for_load_state("domcontentloaded")
					await asyncio.sleep(0.8)
					
					# Диалог не обработан, продолжаем
					
					# 1. СНАЧАЛА ДЕЛАЕМ СКРИНШОТ (чтобы ты видел, что написали)
					if self._screenshot_callback:
						try:
							screenshot_path = "storage/new_message.png"
							await chat_page.screenshot(path=screenshot_path, full_page=False)
							print(f"[FunPay] >> Скриншот сохранён")
							
							# Отправляем скриншот в Telegram
							await self._screenshot_callback(screenshot_path, None)
						except Exception as e:
							print(f"[FunPay] Ошибка скриншота: {e}")
					
					# 2. ПОТОМ ОТПРАВЛЯЕМ АВТООТВЕТ
					# Ищем поле ввода
					editor_selectors = [
						"textarea[name='content']",
						config.dialog_reply_input_selector,
						"textarea#message",
						"textarea",
					]
					
					reply_elem = None
					for sel in editor_selectors:
						try:
							reply_elem = await chat_page.wait_for_selector(sel, timeout=1500)
							if reply_elem:
								break
						except Exception:
							continue
					
					if reply_elem:
						# Заполняем текст
						await reply_elem.fill(config.auto_reply_text)
						print(f"[FunPay] >> Отправляю автоответ: {config.auto_reply_text[:40]}...")
						
						# Нажимаем Enter для отправки
						await reply_elem.press("Enter")
						
						await self._save_session()
						print("[FunPay] >> Автоответ отправлен!")
						
						# Отмечаем диалог как обработанный на 2 минуты
						self._processed_dialogs[dialog_id] = current_time
						self._save_processed_dialogs()  # Сохраняем в файл
						print(f"[FunPay] >> Диалог {dialog_id} отмечен как обработанный на 2 минуты")
					else:
						print("[FunPay] >> Не нашёл поле ввода")
					
					# Ждём перед следующей проверкой
					await asyncio.sleep(2.0)
				else:
					# Нет диалогов, ждём
					print("[FunPay] Диалоги не найдены, жду...")
					await asyncio.sleep(2.0)
					
			except Exception as e:
				print(f"[FunPay] Ошибка мониторинга: {e}")
				import traceback
				traceback.print_exc()
				await asyncio.sleep(5)

	async def start(self) -> None:
		if self._running:
			return
		
		# Загружаем обработанные диалоги
		self._load_processed_dialogs()
		
		self._running = True
		# Запускаем только цикл услуг (автоответ встроен в него)
		asyncio.create_task(self._services_auto_post_loop())

	async def stop(self) -> None:
		self._running = False

	async def analyze_currency_prices(self) -> str:
		"""Анализ цен на валюту Minecraft для сервера FunTime"""
		try:
			# Используем основную страницу или создаем новую
			page = self._page
			if not page or page.is_closed():
				await self.launch()
				page = self._page
			
			if not page:
				return "❌ Не удалось открыть браузер"
			
			print("[FunPay] Переходим на страницу валюты Minecraft...")
			await page.goto("https://funpay.com/lots/1596/", wait_until="domcontentloaded")
			await asyncio.sleep(2)
			
			# Ищем выпадающий список серверов
			print("[FunPay] Ищем выпадающий список серверов...")
			server_selectors = [
				"select[name='server']",
				".server-select",
				"select",
				"[data-server]"
			]
			
			server_select = None
			for selector in server_selectors:
				try:
					server_select = await page.query_selector(selector)
					if server_select:
						print(f"[FunPay] Найден селектор серверов: {selector}")
						break
				except Exception:
					continue
			
			if not server_select:
				# Попробуем найти кнопку или другой элемент для выбора сервера
				server_button_selectors = [
					"button:has-text('Сервер')",
					".server-button",
					"[data-testid='server-select']"
				]
				
				for selector in server_button_selectors:
					try:
						server_button = await page.query_selector(selector)
						if server_button:
							print(f"[FunPay] Найдена кнопка сервера: {selector}")
							await server_button.click()
							await asyncio.sleep(1)
							break
					except Exception:
						continue
			
			# Ищем опцию FunTime
			print("[FunPay] Ищем опцию FunTime...")
			funtime_selectors = [
				"option:has-text('FunTime')",
				"option[value*='FunTime']",
				"[data-value*='FunTime']",
				"li:has-text('FunTime')",
				"div:has-text('FunTime')"
			]
			
			funtime_option = None
			for selector in funtime_selectors:
				try:
					funtime_option = await page.query_selector(selector)
					if funtime_option:
						print(f"[FunPay] Найдена опция FunTime: {selector}")
						break
				except Exception:
					continue
			
			if funtime_option:
				try:
					# Пробуем выбрать через JavaScript
					await page.evaluate("""
						() => {
							const select = document.querySelector('select');
							if (select) {
								// Ищем опцию FunTime
								for (let option of select.options) {
									if (option.textContent.includes('FunTime')) {
										option.selected = true;
										select.dispatchEvent(new Event('change'));
										return true;
									}
								}
							}
							return false;
						}
					""")
					await asyncio.sleep(2)
					print("[FunPay] Выбран сервер FunTime через JavaScript")
				except Exception as e:
					print(f"[FunPay] Ошибка выбора сервера: {e}")
					print("[FunPay] Анализируем все доступные цены")
			else:
				print("[FunPay] Не удалось найти опцию FunTime, анализируем все доступные цены")
			
			# Ждем загрузки результатов
			await asyncio.sleep(3)
			
			# Ищем цены на странице
			print("[FunPay] Анализируем цены...")
			
			# Сначала попробуем найти цены через JavaScript
			try:
				js_prices = await page.evaluate("""
					() => {
						const prices = [];
						const elements = document.querySelectorAll('*');
						
						for (let el of elements) {
							const text = el.textContent || '';
							// Ищем паттерны цен
							const priceRegex = /(\d+\.?\d*)\s*₽/g;
							let match;
							while ((match = priceRegex.exec(text)) !== null) {
								const price = parseFloat(match[1]);
								if (price >= 0.001 && price <= 100) {
									prices.push(price);
								}
							}
						}
						
						return prices;
					}
				""")
				
				if js_prices:
					prices = js_prices
					print(f"[FunPay] Найдено {len(prices)} цен через JavaScript")
				else:
					prices = []
			except Exception as e:
				print(f"[FunPay] Ошибка JavaScript поиска: {e}")
				prices = []
			
			# Если JavaScript не сработал, используем обычный поиск
			if not prices:
				price_selectors = [
					".price",
					"[class*='price']",
					".cost",
					"[class*='cost']",
					".amount",
					"[class*='amount']"
				]
				
				for selector in price_selectors:
					try:
						elements = await page.query_selector_all(selector)
						for element in elements:
							text = await element.inner_text()
							if text and any(char.isdigit() for char in text):
								# Извлекаем числовое значение
								import re
								numbers = re.findall(r'[\d,]+\.?\d*', text)
								for num_str in numbers:
									try:
										price = float(num_str.replace(',', '.'))
										if 0.001 <= price <= 100:  # Разумный диапазон цен
											prices.append(price)
									except ValueError:
										continue
					except Exception:
						continue
			
			if not prices:
				# Попробуем найти цены в тексте страницы
				page_content = await page.content()
				import re
				price_patterns = [
					r'(\d+\.?\d*)\s*₽',
					r'(\d+\.?\d*)\s*руб',
					r'(\d+\.?\d*)\s*р\.',
					r'(\d+\.?\d*)\s*рублей'
				]
				
				for pattern in price_patterns:
					matches = re.findall(pattern, page_content, re.IGNORECASE)
					for match in matches:
						try:
							price = float(match)
							if 0.001 <= price <= 100:
								prices.append(price)
						except ValueError:
							continue
			
			if not prices:
				return "❌ Цены не найдены. Попробуйте позже."
			
			# Анализируем цены
			prices.sort()
			min_price = prices[0]
			max_price = prices[-1]
			avg_price = sum(prices) / len(prices)
			
			# Находим медиану
			n = len(prices)
			if n % 2 == 0:
				median = (prices[n//2-1] + prices[n//2]) / 2
			else:
				median = prices[n//2]
			
			# Умная рекомендация цены
			if min_price < 0.1:  # Если минимальная цена меньше 10 копеек
				# Рекомендуем цену на 1-2 копейки ниже минимальной
				recommended_price = max(min_price - 0.01, 0.01)
			elif min_price < 1:  # Если минимальная цена меньше 1 рубля
				# Рекомендуем цену на 5-10% ниже
				recommended_price = max(min_price * 0.9, 0.01)
			else:  # Если минимальная цена больше 1 рубля
				# Рекомендуем цену на 10-15% ниже
				recommended_price = min_price * 0.85
			
			# Форматируем цены для понятности
			def format_price(price):
				if price < 1:
					kopecks = int(price * 100)
					return f"{kopecks} копеек"
				else:
					rubles = int(price)
					kopecks = int((price - rubles) * 100)
					if kopecks == 0:
						return f"{rubles} рублей"
					else:
						return f"{rubles} руб {kopecks} коп"
			
			result = f"""🔍 **FunTime анализ**

📊 **Цены за 1кк валюты:**
• Минимальная: {format_price(min_price)}
• Максимальная: {format_price(max_price)}
• Средняя: {format_price(avg_price)}
• Всего предложений: {len(prices)}

💡 **Рекомендация: {format_price(recommended_price)}** за 1кк
⬇️ На {((min_price - recommended_price) / min_price * 100):.0f}% ниже минимальной!"""
			
			return result
			
		except Exception as e:
			print(f"[FunPay] Ошибка анализа цен: {e}")
			return f"❌ Ошибка: {str(e)[:50]}..."

	async def analyze_account_prices(self, donate_name: str = None) -> str:
		"""Анализ цен на аккаунты Minecraft для сервера FunTime"""
		try:
			# Используем основную страницу или создаем новую
			page = self._page
			if not page or page.is_closed():
				await self.launch()
				page = self._page
			
			if not page:
				return "❌ Не удалось открыть браузер"
			
			print("[FunPay] Переходим на страницу аккаунтов Minecraft...")
			await page.goto("https://funpay.com/lots/221/", wait_until="domcontentloaded")
			await asyncio.sleep(2)
			
			# Ищем выпадающий список серверов
			print("[FunPay] Ищем выпадающий список серверов...")
			server_selectors = [
				"select[name='server']",
				".server-select",
				"select",
				"[data-server]"
			]
			
			server_select = None
			for selector in server_selectors:
				try:
					server_select = await page.query_selector(selector)
					if server_select:
						print(f"[FunPay] Найден селектор серверов: {selector}")
						break
				except Exception:
					continue
			
			if server_select:
				try:
					# Пробуем выбрать FunTime через JavaScript
					await page.evaluate("""
						() => {
							const select = document.querySelector('select');
							if (select) {
								// Ищем опцию FunTime
								for (let option of select.options) {
									if (option.textContent.includes('FunTime')) {
										option.selected = true;
										select.dispatchEvent(new Event('change'));
										return true;
									}
								}
							}
							return false;
						}
					""")
					await asyncio.sleep(2)
					print("[FunPay] Выбран сервер FunTime")
				except Exception as e:
					print(f"[FunPay] Ошибка выбора сервера: {e}")
			
			# Ждем загрузки результатов
			await asyncio.sleep(3)
			
			# Определяем название доната для поиска
			donate_display = {
				"герцог": "Герцог навсегда",
				"князь": "Князь навсегда", 
				"глава": "Глава",
				"титан": "Титан",
				"элита": "Элита",
				"принц": "Принц"
			}
			
			search_donate = donate_display.get(donate_name, donate_name) if donate_name else None
			print(f"[FunPay] Ищем аккаунты с донатом: {search_donate}")
			
			# Сначала попробуем найти цены через JavaScript
			try:
				js_prices = await page.evaluate(f"""
					() => {{
						const prices = [];
						const searchDonate = '{search_donate}';
						
						// Ищем все карточки товаров на FunPay
						const productCards = document.querySelectorAll('tr, .lot-item, .product-item, [class*="lot"], [class*="item"], [class*="product"], .row .col, .card, .lot');
						
						console.log('Найдено карточек:', productCards.length);
						console.log('Ищем донат:', searchDonate);
						
						// Сначала фильтруем карточки по донату
						const filteredCards = [];
						for (let card of productCards) {{
							const text = card.textContent || '';
							
							// Если ищем конкретный донат, проверяем его наличие в описании
							if (searchDonate && text.toLowerCase().includes(searchDonate.toLowerCase())) {{
								filteredCards.push(card);
								console.log('Найдена карточка с донатом:', text.substring(0, 100));
							}}
						}}
						
						console.log('Отфильтровано карточек с донатом:', filteredCards.length);
						
						if (filteredCards.length === 0) {{
							console.log('Не найдено карточек с донатом:', searchDonate);
							console.log('Попробуем поиск по всей странице...');
							
							// Если не нашли карточки с донатом, ищем по всей странице
							const elements = document.querySelectorAll('*');
							for (let el of elements) {{
								const text = el.textContent || '';
								
								// Если ищем конкретный донат, проверяем его наличие
								if (searchDonate && text.toLowerCase().includes(searchDonate.toLowerCase())) {{
									// Ищем паттерны цен
									const priceRegex = /(\\d+\\.?\\d*)\\s*₽/g;
									let match;
									while ((match = priceRegex.exec(text)) !== null) {{
										const price = parseFloat(match[1]);
										if (price >= 10 && price <= 10000) {{  // Разумный диапазон для аккаунтов
											prices.push(price);
										}}
									}}
								}}
							}}
							return prices;
						}}
						
						// Теперь ищем цены только в отфильтрованных карточках
						for (let card of filteredCards) {{
							const text = card.textContent || '';
							
							// Ищем цену в этой карточке
							const priceElements = card.querySelectorAll('[class*="price"], [class*="cost"], .price, .cost, td:last-child, .text-end');
							console.log('Найдено элементов цен:', priceElements.length);
							
							// Если не нашли элементы цен, ищем по тексту в карточке
							if (priceElements.length === 0) {{
								console.log('Ищем цену по тексту в карточке');
								const priceRegex = /(\\d+\\.?\\d*)\\s*₽/g;
								let match;
								while ((match = priceRegex.exec(text)) !== null) {{
									const price = parseFloat(match[1]);
									console.log('Найдена цена в тексте:', price);
									if (price >= 10 && price <= 10000) {{  // Разумный диапазон для аккаунтов
										prices.push(price);
									}}
								}}
							}} else {{
								for (let priceEl of priceElements) {{
									const priceText = priceEl.textContent || '';
									console.log('Текст цены:', priceText);
									const priceRegex = /(\\d+\\.?\\d*)\\s*₽/g;
									let match;
									while ((match = priceRegex.exec(priceText)) !== null) {{
										const price = parseFloat(match[1]);
										console.log('Найдена цена:', price);
										if (price >= 10 && price <= 10000) {{  // Разумный диапазон для аккаунтов
											prices.push(price);
										}}
									}}
								}}
							}}
						}}
						
						console.log('Итого найдено цен:', prices.length);
						
						// Если не нашли цены, делаем скриншот для отладки
						if (prices.length === 0) {{
							console.log('Не найдено цен, делаем скриншот для отладки');
						}}
						
						return prices;
					}}
				""")
				
				if js_prices:
					prices = js_prices
					print(f"[FunPay] Найдено {len(prices)} цен через JavaScript")
					if len(prices) > 0:
						print(f"[FunPay] Первые 5 цен: {prices[:5]}")
						print(f"[FunPay] Последние 5 цен: {prices[-5:]}")
					else:
						print("[FunPay] JavaScript вернул пустой массив цен")
				else:
					prices = []
			except Exception as e:
				print(f"[FunPay] Ошибка JavaScript поиска: {e}")
				prices = []
			
			if not prices:
				print("[FunPay] Цены не найдены, делаем скриншот для отладки")
				try:
					await page.screenshot(path="debug_accounts_no_prices.png")
					print("[FunPay] Скриншот сохранен: debug_accounts_no_prices.png")
				except Exception as e:
					print(f"[FunPay] Ошибка скриншота: {e}")
				return f"❌ Цены на аккаунты с донатом '{search_donate}' не найдены. Попробуйте позже."
			
			# Анализируем цены
			print(f"[FunPay] Начинаем анализ {len(prices)} цен")
			
			# Если цен слишком много, возможно ищем не только нужный донат
			if len(prices) > 1000:
				print(f"[FunPay] ВНИМАНИЕ: Найдено {len(prices)} цен - возможно ищем не только нужный донат!")
				print(f"[FunPay] Первые 10 цен: {prices[:10]}")
				print(f"[FunPay] Последние 10 цен: {prices[-10:]}")
				
				# Ограничиваем количество цен для анализа
				prices = prices[:100]  # Берем только первые 100 цен
				print(f"[FunPay] Ограничиваем анализ до {len(prices)} цен")
			elif len(prices) < 5:
				print(f"[FunPay] ВНИМАНИЕ: Найдено только {len(prices)} цен - возможно ищем не тот донат!")
				print(f"[FunPay] Все найденные цены: {prices}")
				print(f"[FunPay] Ищем донат: {search_donate}")
			else:
				print(f"[FunPay] Найдено {len(prices)} цен - нормальное количество")
				
			# Делаем скриншот для отладки
			try:
				await page.screenshot(path="debug_accounts_found_prices.png")
				print("[FunPay] Скриншот с найденными ценами сохранен: debug_accounts_found_prices.png")
			except Exception as e:
				print(f"[FunPay] Ошибка скриншота: {e}")
				
			# Добавляем отладочную информацию
			print(f"[FunPay] Ищем донат: {search_donate}")
			print(f"[FunPay] Найдено цен: {len(prices)}")
			if len(prices) > 0:
				print(f"[FunPay] Диапазон цен: {min(prices)} - {max(prices)}")
			
			# Сортируем цены для анализа
			prices.sort()
			min_price = prices[0]
			max_price = prices[-1]
			avg_price = sum(prices) / len(prices)
			
			print(f"[FunPay] После сортировки: мин={min_price}, макс={max_price}, средняя={avg_price:.2f}")
			
			# Умная рекомендация цены
			if min_price < 100:  # Если минимальная цена меньше 100 рублей
				# Рекомендуем цену на 10-20 рублей ниже минимальной
				recommended_price = max(min_price - 15, 50)
			elif min_price < 500:  # Если минимальная цена меньше 500 рублей
				# Рекомендуем цену на 5-10% ниже
				recommended_price = min_price * 0.9
			else:  # Если минимальная цена больше 500 рублей
				# Рекомендуем цену на 10-15% ниже
				recommended_price = min_price * 0.85
			
			print(f"[FunPay] Рекомендуемая цена: {recommended_price}")
			
			# Форматируем цены для понятности
			def format_price(price):
				if price < 100:
					return f"{price:.0f} руб"
				else:
					rubles = int(price)
					kopecks = int((price - rubles) * 100)
					if kopecks == 0:
						return f"{rubles} руб"
					else:
						return f"{rubles} руб {kopecks} коп"
			
			donate_title = search_donate if search_donate else "все донаты"
			result = f"""🔍 **FunTime аккаунты анализ - {donate_title}**

📊 **Цены на аккаунты:**
• Минимальная: {format_price(min_price)}
• Максимальная: {format_price(max_price)}
• Средняя: {format_price(avg_price)}
• Всего предложений: {len(prices)}

💡 **Рекомендация: {format_price(recommended_price)}** за аккаунт
⬇️ На {((min_price - recommended_price) / min_price * 100):.0f}% ниже минимальной!

🎯 **Анализировался донат:** {donate_title}"""
			
			print(f"[FunPay] Результат: {result[:100]}...")
			
			return result
			
		except Exception as e:
			print(f"[FunPay] Ошибка анализа цен аккаунтов: {e}")
			import traceback
			traceback.print_exc()
			return f"❌ Ошибка: {str(e)[:50]}..."

	async def find_cheapest_account(self, donate_name: str) -> str:
		"""Поиск самого дешевого аккаунта с донатом"""
		try:
			# Используем основную страницу или создаем новую
			page = self._page
			if not page or page.is_closed():
				page = await self._ensure_orders_page()
			
			# Переходим на страницу покупки аккаунтов (не продаж!)
			await page.goto("https://funpay.com/lots/221/?type=buy", wait_until="domcontentloaded")
			await asyncio.sleep(2)
			
			# Выбираем сервер FunTime
			try:
				await page.evaluate("""
					() => {
						const serverSelect = document.querySelector('select[name="server"]');
						if (serverSelect) {
							const funtimeOption = Array.from(serverSelect.options).find(option => 
								option.textContent.toLowerCase().includes('funtime') || 
								option.textContent.toLowerCase().includes('fun time')
							);
							if (funtimeOption) {
								funtimeOption.selected = true;
								serverSelect.dispatchEvent(new Event('change'));
							}
						}
					}
				""")
				await asyncio.sleep(2)
			except Exception as e:
				print(f"[FunPay] Ошибка выбора сервера: {e}")
			
			# Определяем название доната для поиска
			donate_display = {
				"герцог": "Герцог навсегда",
				"князь": "Князь навсегда", 
				"глава": "Глава",
				"титан": "Титан",
				"элита": "Элита",
				"принц": "Принц"
			}
			
			search_donate = donate_display.get(donate_name, donate_name)
			print(f"[FunPay] Ищем самый дешевый аккаунт с донатом: {search_donate}")
			
			# Ищем все аккаунты с этим донатом
			accounts = await page.evaluate(f"""
				() => {{
					const accounts = [];
					const searchDonate = '{search_donate}';
					
					// Ищем все карточки товаров
					const productCards = document.querySelectorAll('tr, .lot-item, .product-item, [class*="lot"], [class*="item"], [class*="product"], .row .col, .card, .lot');
					
					for (let card of productCards) {{
						const text = card.textContent || '';
						
						// Проверяем наличие доната в описании
						if (text.toLowerCase().includes(searchDonate.toLowerCase())) {{
							// Ищем цену
							const priceRegex = /(\\d+\\.?\\d*)\\s*₽/g;
							let match;
							let price = null;
							while ((match = priceRegex.exec(text)) !== null) {{
								const p = parseFloat(match[1]);
								if (p >= 10 && p <= 10000) {{
									price = p;
									break;
								}}
							}}
							
							// Ищем ссылку на лот
							const linkElement = card.querySelector('a[href*="/lot/"]');
							let link = null;
							if (linkElement) {{
								link = linkElement.href;
							}} else {{
								// Ищем ссылку в родительских элементах
								let parent = card.parentElement;
								while (parent && !link) {{
									const parentLink = parent.querySelector('a[href*="/lot/"]');
									if (parentLink) {{
										link = parentLink.href;
										break;
									}}
									parent = parent.parentElement;
								}}
							}}
							
							if (price && link) {{
								accounts.push({{
									price: price,
									link: link,
									description: text.substring(0, 200)
								}});
							}}
						}}
					}}
					
					// Сортируем по цене
					accounts.sort((a, b) => a.price - b.price);
					
					return accounts;
				}}
			""")
			
			if not accounts:
				return f"❌ Не найдено аккаунтов с донатом '{search_donate}'"
			
			# Берем самый дешевый
			cheapest = accounts[0]
			
			result = f"""🛒 **Самый дешевый аккаунт с донатом {search_donate}**

💰 **Цена:** {cheapest['price']:.0f} руб
🔗 **Ссылка:** {cheapest['link']}
📝 **Описание:** {cheapest['description'][:100]}...

💡 **Всего найдено:** {len(accounts)} аккаунтов
📊 **Диапазон цен:** {min(acc['price'] for acc in accounts):.0f} - {max(acc['price'] for acc in accounts):.0f} руб"""
			
			return result
			
		except Exception as e:
			print(f"[FunPay] Ошибка поиска самого дешевого аккаунта: {e}")
			import traceback
			traceback.print_exc()
			return f"❌ Ошибка: {str(e)[:50]}..."

	async def analyze_sell_price(self, donate_name: str) -> str:
		"""Анализ цены для продажи аккаунта"""
		try:
			# Используем существующий метод анализа цен
			result = await self.analyze_account_prices(donate_name)
			
			# Парсим результат для извлечения цен
			import re
			price_match = re.search(r'Минимальная: (\d+(?:\.\d+)?)', result)
			avg_match = re.search(r'Средняя: (\d+(?:\.\d+)?)', result)
			
			if price_match and avg_match:
				min_price = float(price_match.group(1))
				avg_price = float(avg_match.group(1))
				
				# Рекомендуем цену для продажи
				if min_price < 100:
					recommended_sell = min_price - 10  # На 10 рублей дешевле минимальной
				elif min_price < 500:
					recommended_sell = min_price * 0.85  # На 15% дешевле
				else:
					recommended_sell = min_price * 0.9  # На 10% дешевле
				
				recommended_sell = max(recommended_sell, 50)  # Минимум 50 рублей
				
				sell_result = f"""💰 **Рекомендация цены для продажи**

📊 **Анализ рынка:**
{result}

💡 **Рекомендуемая цена:** {recommended_sell:.0f} руб
📈 **Стратегия:** Конкурентная цена для быстрой продажи
🎯 **Цель:** Продать быстрее конкурентов"""
				
				return sell_result
			else:
				return result
				
		except Exception as e:
			print(f"[FunPay] Ошибка анализа цены продажи: {e}")
			return f"❌ Ошибка: {str(e)[:50]}..."

	async def find_cheapest_account_with_binding(self, donate_name: str, binding_type: str) -> str:
		"""Поиск самого дешевого аккаунта с донатом и типом привязки"""
		try:
			# Используем основную страницу или создаем новую
			page = self._page
			if not page or page.is_closed():
				page = await self._ensure_orders_page()
			
			# Переходим на страницу покупки аккаунтов (не продаж!)
			await page.goto("https://funpay.com/lots/221/?type=buy", wait_until="domcontentloaded")
			await asyncio.sleep(2)
			
			# Выбираем сервер FunTime
			try:
				await page.evaluate("""
					() => {
						const serverSelect = document.querySelector('select[name="server"]');
						if (serverSelect) {
							const funtimeOption = Array.from(serverSelect.options).find(option => 
								option.textContent.toLowerCase().includes('funtime') || 
								option.textContent.toLowerCase().includes('fun time')
							);
							if (funtimeOption) {
								funtimeOption.selected = true;
								serverSelect.dispatchEvent(new Event('change'));
							}
						}
					}
				""")
				await asyncio.sleep(2)
			except Exception as e:
				print(f"[FunPay] Ошибка выбора сервера: {e}")
			
			# Определяем название доната для поиска
			donate_display = {
				"герцог": "Герцог навсегда",
				"князь": "Князь навсегда", 
				"глава": "Глава",
				"титан": "Титан",
				"элита": "Элита",
				"принц": "Принц"
			}
			
			search_donate = donate_display.get(donate_name, donate_name)
			print(f"[FunPay] Ищем аккаунт с донатом: {search_donate}, тип привязки: {binding_type}")
			
			# Ищем поле поиска и вводим текст
			try:
				search_input = await page.wait_for_selector('input[type="text"], input[placeholder*="поиск"], input[placeholder*="search"]', timeout=5000)
				if search_input:
					await search_input.fill(f"*{search_donate}*")
					await search_input.press("Enter")
					await asyncio.sleep(3)
					print(f"[FunPay] Выполнен поиск по: *{search_donate}*")
				else:
					print("[FunPay] Поле поиска не найдено")
			except Exception as e:
				print(f"[FunPay] Ошибка поиска: {e}")
			
			# Убеждаемся, что мы на странице покупки, а не продажи
			try:
				await page.evaluate("""
					() => {
						// Ищем кнопку "Купить" или переключатель "Покупка"
						const buyButton = document.querySelector('button:has-text("Купить"), a:has-text("Купить")');
						if (buyButton) {
							buyButton.click();
						}
						
						// Ищем переключатель "Покупка" vs "Продажа"
						const buyTab = document.querySelector('a:has-text("Покупка"), button:has-text("Покупка")');
						if (buyTab) {
							buyTab.click();
						}
					}
				""")
				await asyncio.sleep(2)
				print("[FunPay] Переключились на режим покупки")
				
				# Делаем скриншот для отладки
				await page.screenshot(path="debug_buy_mode.png")
				print("[FunPay] Скриншот режима покупки: debug_buy_mode.png")
			except Exception as e:
				print(f"[FunPay] Ошибка переключения на покупку: {e}")
			
			# Ищем все аккаунты с этим донатом и типом привязки
			accounts = await page.evaluate(f"""
				() => {{
					const accounts = [];
					const searchDonate = '{search_donate}';
					const bindingType = '{binding_type}';
					
					console.log('Ищем донат:', searchDonate);
					console.log('Тип привязки:', bindingType);
					console.log('Текущий URL:', window.location.href);
					
					// Ищем все карточки товаров для покупки (не для продажи!)
					const productCards = document.querySelectorAll('tr, .lot-item, .product-item, [class*="lot"], [class*="item"], [class*="product"], .row .col, .card, .lot');
					console.log('Найдено карточек:', productCards.length);
					
					// Фильтруем только карточки для покупки (исключаем продажи)
					const buyCards = [];
					for (let card of productCards) {
						const text = card.textContent || '';
						// Исключаем карточки с текстом "Продажа" или "Продаю"
						if (!text.toLowerCase().includes('продажа') && 
							!text.toLowerCase().includes('продаю') &&
							!text.toLowerCase().includes('продам')) {
							buyCards.push(card);
						}
					}
					console.log('Отфильтровано карточек для покупки:', buyCards.length);
					
					// Показываем первые несколько карточек для отладки
					for (let i = 0; i < Math.min(3, buyCards.length); i++) {{
						console.log('Карточка', i+1, ':', buyCards[i].textContent.substring(0, 100));
					}}
					
					for (let card of buyCards) {{
						const text = card.textContent || '';
						
						// Проверяем наличие доната в описании (ищем точное совпадение)
						const hasDonate = text.toLowerCase().includes(searchDonate.toLowerCase());
						
						// Если не нашли точное совпадение, ищем частичные совпадения
						let hasPartialDonate = false;
						if (!hasDonate) {{
							// Ищем частичные совпадения для разных донатов
							if (searchDonate.includes('Князь')) {{
								hasPartialDonate = text.toLowerCase().includes('князь') && text.toLowerCase().includes('навсегда');
							}} else if (searchDonate.includes('Герцог')) {{
								hasPartialDonate = text.toLowerCase().includes('герцог') && text.toLowerCase().includes('навсегда');
							}} else if (searchDonate.includes('Глава')) {{
								hasPartialDonate = text.toLowerCase().includes('глава');
							}} else if (searchDonate.includes('Титан')) {{
								hasPartialDonate = text.toLowerCase().includes('титан');
							}} else if (searchDonate.includes('Элита')) {{
								hasPartialDonate = text.toLowerCase().includes('элита');
							}} else if (searchDonate.includes('Принц')) {{
								hasPartialDonate = text.toLowerCase().includes('принц');
							}}
						}}
						
						if (hasDonate || hasPartialDonate) {{
							// Проверяем тип привязки
							let matchesBinding = false;
							if (bindingType === 'any') {{
								matchesBinding = true;
							}} else if (bindingType === 'with') {{
								// С привязкой - ищем слова "привязка", "привязан", "привязан к"
								matchesBinding = text.toLowerCase().includes('привязка') || 
												text.toLowerCase().includes('привязан') ||
												text.toLowerCase().includes('привязан к');
							}} else if (bindingType === 'without') {{
								// Без привязки - ищем слова "без привязки", "не привязан"
								matchesBinding = text.toLowerCase().includes('без привязки') || 
												text.toLowerCase().includes('не привязан') ||
												text.toLowerCase().includes('без привязки к');
							}} else if (bindingType === 'lost') {{
								// Утерянная привязка - ищем слова "утерянная", "потерянная"
								matchesBinding = text.toLowerCase().includes('утерянная') || 
												text.toLowerCase().includes('потерянная') ||
												text.toLowerCase().includes('утерян');
							}}
							
							if (matchesBinding) {{
								console.log('Найден подходящий аккаунт:', text.substring(0, 100));
								
								// Ищем цену
								const priceRegex = /(\\d+\\.?\\d*)\\s*₽/g;
								let match;
								let price = null;
								while ((match = priceRegex.exec(text)) !== null) {{
									const p = parseFloat(match[1]);
									if (p >= 10 && p <= 10000) {{
										price = p;
										break;
									}}
								}}
								
								// Ищем ссылку на лот
								const linkElement = card.querySelector('a[href*="/lot/"]');
								let link = null;
								if (linkElement) {{
									link = linkElement.href;
								}} else {{
									// Ищем ссылку в родительских элементах
									let parent = card.parentElement;
									while (parent && !link) {{
										const parentLink = parent.querySelector('a[href*="/lot/"]');
										if (parentLink) {{
											link = parentLink.href;
											break;
										}}
										parent = parent.parentElement;
									}}
								}}
								
								if (price && link) {{
									console.log('Добавляем аккаунт:', price, link);
									accounts.push({{
										price: price,
										link: link,
										description: text.substring(0, 200)
									}});
								}} else {{
									console.log('Не найдена цена или ссылка:', price, link);
								}}
							}}
						}}
					}}
					
					// Сортируем по цене
					accounts.sort((a, b) => a.price - b.price);
					
					console.log('Итого найдено аккаунтов:', accounts.length);
					if (accounts.length > 0) {{
						console.log('Первый аккаунт:', accounts[0]);
					}}
					
					return accounts;
				}}
			""")
			
			print(f"[FunPay] JavaScript вернул {len(accounts) if accounts else 0} аккаунтов")
			
			if accounts:
				print(f"[FunPay] Найдены аккаунты:")
				for i, acc in enumerate(accounts[:3]):  # Показываем первые 3
					print(f"[FunPay] {i+1}. {acc['price']} руб - {acc['description'][:50]}...")
			else:
				print(f"[FunPay] Аккаунты не найдены")
				print(f"[FunPay] Возможные причины:")
				print(f"[FunPay] 1. Неправильный поиск доната: '{search_donate}'")
				print(f"[FunPay] 2. Неправильный тип привязки: '{binding_type}'")
				print(f"[FunPay] 3. Проблемы с селекторами карточек")
				print(f"[FunPay] 4. Проблемы с извлечением цен/ссылок")
				
				# Делаем скриншот для отладки
				try:
					await page.screenshot(path="debug_no_accounts_found.png")
					print("[FunPay] Скриншот сохранен: debug_no_accounts_found.png")
				except Exception as e:
					print(f"[FunPay] Ошибка скриншота: {e}")
			
			if not accounts:
				binding_names = {
					"with": "с привязкой",
					"without": "без привязки", 
					"lost": "с утерянной привязкой",
					"any": "любого типа"
				}
				binding_name = binding_names.get(binding_type, binding_type)
				print(f"[FunPay] Не найдено аккаунтов с донатом '{search_donate}' {binding_name}")
				return f"❌ Не найдено аккаунтов с донатом '{search_donate}' {binding_name}"
			
			# Берем самый дешевый
			cheapest = accounts[0]
			print(f"[FunPay] Самый дешевый аккаунт: {cheapest['price']} руб, {cheapest['link']}")
			
			# Анализируем конкретный лот для определения типа привязки
			if binding_type != "any":
				print(f"[FunPay] Анализируем лот для определения типа привязки...")
				lot_analysis = await self._analyze_lot_binding(cheapest['link'])
				if lot_analysis:
					print(f"[FunPay] Анализ лота: {lot_analysis}")
					
					# Если тип привязки не совпадает, ищем следующий лот
					if binding_type == "without" and "без привязки" not in lot_analysis.lower():
						print(f"[FunPay] Лот не подходит по типу привязки, ищем следующий...")
						# Ищем следующий подходящий лот
						for i, acc in enumerate(accounts[1:], 1):
							lot_analysis = await self._analyze_lot_binding(acc['link'])
							if lot_analysis and "без привязки" in lot_analysis.lower():
								cheapest = acc
								print(f"[FunPay] Найден подходящий лот: {acc['price']} руб")
								break
					elif binding_type == "with" and "привязка" not in lot_analysis.lower():
						print(f"[FunPay] Лот не подходит по типу привязки, ищем следующий...")
						# Ищем следующий подходящий лот
						for i, acc in enumerate(accounts[1:], 1):
							lot_analysis = await self._analyze_lot_binding(acc['link'])
							if lot_analysis and "привязка" in lot_analysis.lower():
								cheapest = acc
								print(f"[FunPay] Найден подходящий лот: {acc['price']} руб")
								break
			
			binding_names = {
				"with": "с привязкой",
				"without": "без привязки", 
				"lost": "с утерянной привязкой",
				"any": "любого типа"
			}
			binding_name = binding_names.get(binding_type, binding_type)
			
			# Проверяем, определен ли тип привязки
			binding_info = ""
			if binding_type != "any":
				lot_analysis = await self._analyze_lot_binding(cheapest['link'])
				if lot_analysis:
					if "без привязки" in lot_analysis.lower():
						binding_info = "✅ **Тип привязки:** Без привязки"
					elif "привязка" in lot_analysis.lower():
						binding_info = "✅ **Тип привязки:** С привязкой"
					else:
						binding_info = "❓ **Тип привязки:** Не определен - нужно уточнить у продавца\n💬 **Сообщение продавцу:** Привет! Аккаунт с привязкой?"
				else:
					binding_info = "❓ **Тип привязки:** Не определен - нужно уточнить у продавца\n💬 **Сообщение продавцу:** Привет! Аккаунт с привязкой?"
			else:
				# Для типа "any" тоже анализируем лот
				lot_analysis = await self._analyze_lot_binding(cheapest['link'])
				if lot_analysis:
					if "без привязки" in lot_analysis.lower():
						binding_info = "✅ **Тип привязки:** Без привязки"
					elif "привязка" in lot_analysis.lower():
						binding_info = "✅ **Тип привязки:** С привязкой"
					else:
						binding_info = "❓ **Тип привязки:** Не определен - нужно уточнить у продавца\n💬 **Сообщение продавцу:** Привет! Аккаунт с привязкой?"
				else:
					binding_info = "❓ **Тип привязки:** Не определен - нужно уточнить у продавца\n💬 **Сообщение продавцу:** Привет! Аккаунт с привязкой?"
			
			result = f"""🛒 **Самый дешевый аккаунт {search_donate} {binding_name}**

💰 **Цена:** {cheapest['price']:.0f} руб
🔗 **Ссылка:** {cheapest['link']}
📝 **Описание:** {cheapest['description'][:100]}...

{binding_info}

💡 **Всего найдено:** {len(accounts)} аккаунтов
📊 **Диапазон цен:** {min(acc['price'] for acc in accounts):.0f} - {max(acc['price'] for acc in accounts):.0f} руб"""
			
			return result
			
		except Exception as e:
			print(f"[FunPay] Ошибка поиска аккаунта с привязкой: {e}")
			import traceback
			traceback.print_exc()
			return f"❌ Ошибка: {str(e)[:50]}..."

	async def _analyze_lot_binding(self, lot_url: str) -> str:
		"""Анализирует лот для определения типа привязки"""
		try:
			page = await self._ensure_orders_page()
			await page.goto(lot_url, wait_until="domcontentloaded")
			await asyncio.sleep(2)
			
			# Извлекаем описание лота
			description = await page.evaluate("""
				() => {
					const description = document.querySelector('.lot-description, .product-description, .description, [class*="description"]');
					if (description) {
						return description.textContent || '';
					}
					return '';
				}
			""")
			
			if description:
				print(f"[FunPay] Описание лота: {description[:100]}...")
				return description
			else:
				print("[FunPay] Описание лота не найдено")
				return ""
				
		except Exception as e:
			print(f"[FunPay] Ошибка анализа лота: {e}")
			return ""

	async def analyze_lot_details(self, lot_url: str) -> str:
		"""Анализ детальной информации о лоте"""
		try:
			# Используем основную страницу или создаем новую
			page = self._page
			if not page or page.is_closed():
				page = await self._ensure_orders_page()
			
			# Переходим на страницу лота
			await page.goto(lot_url, wait_until="domcontentloaded")
			await asyncio.sleep(3)
			
			print(f"[FunPay] Анализируем лот: {lot_url}")
			
			# Извлекаем детальную информацию о лоте
			lot_info = await page.evaluate("""
				() => {
					const info = {};
					
					// Ищем заголовок лота
					const titleElement = document.querySelector('h1, .lot-title, .product-title, [class*="title"]');
					if (titleElement) {
						info.title = titleElement.textContent.trim();
					}
					
					// Ищем цену
					const priceElement = document.querySelector('.price, .cost, [class*="price"], [class*="cost"]');
					if (priceElement) {
						info.price = priceElement.textContent.trim();
					}
					
					// Ищем описание
					const descriptionElement = document.querySelector('.description, .lot-description, [class*="description"]');
					if (descriptionElement) {
						info.description = descriptionElement.textContent.trim();
					}
					
					// Ищем информацию о продавце
					const sellerElement = document.querySelector('.seller, .user, [class*="seller"], [class*="user"]');
					if (sellerElement) {
						info.seller = sellerElement.textContent.trim();
					}
					
					// Ищем рейтинг продавца
					const ratingElement = document.querySelector('.rating, .stars, [class*="rating"], [class*="stars"]');
					if (ratingElement) {
						info.rating = ratingElement.textContent.trim();
					}
					
					// Ищем количество отзывов
					const reviewsElement = document.querySelector('.reviews, .feedback, [class*="reviews"], [class*="feedback"]');
					if (reviewsElement) {
						info.reviews = reviewsElement.textContent.trim();
					}
					
					// Ищем время на сайте
					const timeElement = document.querySelector('.time, .date, [class*="time"], [class*="date"]');
					if (timeElement) {
						info.time = timeElement.textContent.trim();
					}
					
					// Ищем статус онлайн
					const onlineElement = document.querySelector('.online, .status, [class*="online"], [class*="status"]');
					if (onlineElement) {
						info.online = onlineElement.textContent.trim();
					}
					
					// Ищем все текстовое содержимое для поиска ключевых слов
					const allText = document.body.textContent || '';
					info.allText = allText.substring(0, 1000); // Первые 1000 символов
					
					return info;
				}
			""")
			
			# Анализируем информацию о привязке
			binding_info = "❓ Не определено"
			if lot_info.get('allText'):
				text = lot_info['allText'].lower()
				if 'без привязки' in text or 'не привязан' in text:
					binding_info = "🔓 Без привязки"
				elif 'привязка' in text or 'привязан' in text:
					if 'утерянная' in text or 'потерянная' in text:
						binding_info = "❓ Утерянная привязка"
					else:
						binding_info = "🔗 С привязкой"
			
			# Формируем результат
			result = f"""🔍 **Детальный анализ лота**

📋 **Основная информация:**
• **Название:** {lot_info.get('title', 'Не найдено')}
• **Цена:** {lot_info.get('price', 'Не найдено')}
• **Тип привязки:** {binding_info}

👤 **Продавец:**
• **Имя:** {lot_info.get('seller', 'Не найдено')}
• **Рейтинг:** {lot_info.get('rating', 'Не найден')}
• **Отзывы:** {lot_info.get('reviews', 'Не найдено')}
• **Время на сайте:** {lot_info.get('time', 'Не найдено')}
• **Статус:** {lot_info.get('online', 'Не определен')}

📝 **Описание:**
{lot_info.get('description', 'Не найдено')[:300]}...

🔗 **Ссылка на лот:** {lot_url}"""
			
			return result
			
		except Exception as e:
			print(f"[FunPay] Ошибка анализа лота: {e}")
			import traceback
			traceback.print_exc()
			return f"❌ Ошибка анализа лота: {str(e)[:50]}..."