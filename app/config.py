import os
from dataclasses import dataclass
from dotenv import load_dotenv
from typing import List


load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
	value = os.getenv(name)
	if value is None:
		return default
	return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
	value = os.getenv(name)
	try:
		return int(value) if value is not None else default
	except ValueError:
		return default


@dataclass
class AppConfig:
	funpay_base_url: str = os.getenv("FUNPAY_BASE_URL", "https://funpay.com/")
	funpay_section_url: str = os.getenv("FUNPAY_SECTION_URL", "https://funpay.com/lots/223/")
	storage_path: str = os.getenv("FUNPAY_STORAGE_PATH", "storage/funpay.json")

	post_text: str = os.getenv("POST_TEXT") or "Привет! Выполняю услуги по Minecraft. Напишите, что нужно сделать."
	post_interval_minutes: int = _env_int("POST_INTERVAL_MINUTES", 5)
	services_interval: int = _env_int("SERVICES_INTERVAL", 5)  # Интервал для услуг в секундах
	headless: bool = _env_bool("HEADLESS", True)

	auto_reply_enabled: bool = _env_bool("AUTO_REPLY_ENABLED", True)
	auto_reply_text: str = os.getenv("AUTO_REPLY_TEXT") or "Здравствуйте! Опишите задачу, версию и бюджет."
	auto_reply_check_sec: int = _env_int("AUTO_REPLY_CHECK_SEC", 15)

	chat_input_selector: str = os.getenv("CHAT_INPUT_SELECTOR", "textarea")
	chat_send_selector: str = os.getenv("CHAT_SEND_SELECTOR", "button[type=\"submit\"],button.send")

	# Диалоги и форма отправки сообщений (по присланному HTML)
	unread_dialog_selector: str = os.getenv("UNREAD_DIALOG_SELECTOR", ".contact-list a.contact-item.unread")
	dialog_open_click_selector: str = os.getenv("DIALOG_OPEN_CLICK_SELECTOR", "a, .contact-item")
	dialog_reply_input_selector: str = os.getenv("DIALOG_REPLY_INPUT_SELECTOR", "textarea[name='content']")
	dialog_reply_send_selector: str = os.getenv("DIALOG_REPLY_SEND_SELECTOR", ".chat-form-btn button[type='submit']")

	# Селектор баланса
	balance_selector: str = os.getenv("BALANCE_SELECTOR", "[data-balance], .balance, .header-balance")

	preset_replies_raw: str = os.getenv("PRESET_REPLIES", "Здравствуйте! Чем могу помочь?|Готов взяться, напишите детали.|Сделаю быстро и качественно.")

	telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
	telegram_admin_id: int = _env_int("TELEGRAM_ADMIN_ID", 0)

	def preset_replies(self) -> List[str]:
		return [x.strip() for x in self.preset_replies_raw.split("|") if x.strip()]


config = AppConfig()
