# main.py
import asyncio
import logging
import yaml
from pathlib import Path
from watchdog.observers import Observer
from core.tag_registry import TagRegistry, ConfigWatcher
from drivers.modbus_tcp import ModbusTcpDriver
from core.opcua_server import OPCUAServer

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(name)-12s │ %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("Server")

async def main():
    cfg_path = Path("config/tags.yaml")
    if not cfg_path.exists():
        logger.error(f"Файл конфигурации не найден: {cfg_path}")
        return

    # 1. Реестр тегов
    registry = TagRegistry()
    registry.load_config(cfg_path)
    logger.info(f"Загружено тегов: {len(registry.tags)}")

    # 2. Hot-Reload конфигуратора
    event_handler = ConfigWatcher(registry, cfg_path)
    observer = Observer()
    observer.schedule(event_handler, path=str(cfg_path.parent), recursive=False)
    observer.start()
    logger.info("Мониторинг конфигурации запущен")

    # 3. Инициализация драйверов
    drivers_cfg = yaml.safe_load(cfg_path.read_text()).get("drivers", [])
    drivers = {}
    for d_cfg in drivers_cfg:
        if d_cfg["type"] == "modbus_tcp":
            driver = ModbusTcpDriver(d_cfg)
            drivers[d_cfg["name"]] = driver
            logger.info(f"Создан драйвер {d_cfg['name']} → {d_cfg['host']}:{d_cfg['port']}")

    # 4. Запуск опросов
    poll_tasks = []
    for name, driver in drivers.items():
        interval = next(d["poll_interval"] for d in drivers_cfg if d["name"] == name)
        await driver.connect()  # Пробуем подключиться сразу
        poll_tasks.append(asyncio.create_task(driver.poll_loop(registry, interval)))
        logger.info(f"Опрос {name} запущен (интервал {interval}с)")

    # 5. OPC UA Server (раскомментируйте, когда установите asyncua)
    opc = OPCUAServer(registry)
    await opc.start()
    poll_tasks.append(asyncio.create_task(opc.sync_from_registry()))
    logger.info("OPC UA сервер запущен на opc.tcp://0.0.0.0:4840")

    logger.info("═══════════════════════════════════════════")
    logger.info("Сервер работает. Нажмите Ctrl+C для остановки.")
    logger.info("═══════════════════════════════════════════")

    try:
        await asyncio.gather(*poll_tasks)
    except asyncio.CancelledError:
        logger.info("Получен сигнал остановки...")
    finally:
        # Graceful shutdown
        observer.stop()
        observer.join()
        for d in drivers.values():
            await d.disconnect()
        # if opc: await opc.server.stop()
        logger.info("Сервер остановлен.")

if __name__ == "__main__":
    # Корректная обработка Ctrl+C в asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()