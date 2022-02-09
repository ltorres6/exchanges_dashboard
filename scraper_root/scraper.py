import logging
import os
from types import SimpleNamespace

import hjson
from scraper.binancefutures import BinanceFutures
from scraper.bybitderivatives import BybitDerivatives
from scraper_root.scraper.binancespot import BinanceSpot
from scraper_root.scraper.data_classes import ScraperConfig
from scraper_root.scraper.persistence.repository import Repository

logging.basicConfig(format="%(asctime)s %(levelname)-8s %(message)s", level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")

logger = logging.getLogger()

if __name__ == "__main__":
    config_file_path = os.environ["CONFIG_FILE"]
    logger.info(f"Using config file {config_file_path}")
    with open(config_file_path) as config_file:
        user_config = hjson.load(config_file, object_hook=lambda d: SimpleNamespace(**d))

    scraper_config = ScraperConfig()
    for key in user_config:
        if hasattr(scraper_config, key):
            setattr(scraper_config, key, user_config[key])

    if "BTCUSDT" not in scraper_config.symbols:
        scraper_config.symbols.append("BTCUSDT")

    scraper = None
    repository = Repository()
    if scraper_config.exchange == "binance_futures":
        scraper = BinanceFutures(config=scraper_config, repository=repository)
    elif scraper_config.exchange == "binance_spot":
        scraper = BinanceSpot(config=scraper_config, repository=repository)
    elif scraper_config.exchange == "binance_us":
        scraper = BinanceSpot(config=scraper_config, repository=repository, exchange="binance.us")
    elif scraper_config.exchange == "bybit_derivatives":
        scraper = BybitDerivatives(config=scraper_config, repository=repository)

    try:
        scraper.start()
    except Exception as e:
        logger.error(f"Failed to start exchange: {e}")
