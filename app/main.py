import asyncio
from .config import config
from .funpay_client import FunPayClient
from .telegram_bot import run_telegram


async def main() -> None:
	client = FunPayClient()
	await client.launch()
	# Автозапуск не включаем, управляем из Telegram
	try:
		await run_telegram(client)
	finally:
		await client.close()


if __name__ == "__main__":
	asyncio.run(main())


