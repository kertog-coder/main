import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("FUNPAY_BASE_URL", "https://funpay.com/")
STORAGE_PATH = os.getenv("FUNPAY_STORAGE_PATH", "storage/funpay.json")


async def main() -> None:
	Path(os.path.dirname(STORAGE_PATH) or ".").mkdir(parents=True, exist_ok=True)
	pw = await async_playwright().start()
	browser = await pw.chromium.launch(headless=False)
	context = await browser.new_context()
	page = await context.new_page()
	print("Открылось окно Chromium. Перейду на FunPay...")
	await page.goto(BASE_URL)
	print("Авторизуйтесь в FunPay, затем закройте окно браузера.")
	try:
		await page.wait_for_event("close")
	except Exception:
		pass
	finally:
		try:
			await context.storage_state(path=STORAGE_PATH)
			print(f"Сессия сохранена в {STORAGE_PATH}")
		except Exception as e:
			print(f"Не удалось сохранить сессию: {e}")
		await browser.close()
		await pw.stop()


if __name__ == "__main__":
	asyncio.run(main())

