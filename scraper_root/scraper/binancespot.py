import datetime
import logging
import threading
import time
from typing import List

from unicorn_binance_rest_api import BinanceRestApiManager
from unicorn_binance_websocket_api import BinanceWebSocketApiManager

from scraper_root.scraper.data_classes import AssetBalance, Position, ScraperConfig, Tick, Balance, \
    Income, Order, Trade
from scraper_root.scraper.persistence.orm_classes import TradeEntity
from scraper_root.scraper.persistence.repository import Repository

logger = logging.getLogger()


class BinanceSpot:
    def __init__(self, config: ScraperConfig, repository: Repository, exchange: str = "binance.com"):
        print('Binance spot initialized')
        self.config = config
        self.api_key = self.config.api_key
        self.secret = self.config.api_secret
        self.repository = repository
        self.ws_manager = BinanceWebSocketApiManager(exchange=exchange, throw_exception_if_unrepairable=True,
                                                     warn_on_update=False)

        self.rest_manager = BinanceRestApiManager(self.api_key, api_secret=self.secret, exchange=exchange)
        self.exchange_information = None
        self.tick_symbols = []

    def start(self):
        print('Starting binance spot scraper')

        self.exchange_information = self.rest_manager.get_exchange_info()
        sorted_symbols = [s for s in self.exchange_information['symbols'] if
                          s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT']
        sorted_symbols.extend([s for s in self.exchange_information['symbols'] if s not in sorted_symbols])
        self.exchange_information['symbols'] = sorted_symbols
        symbol_search_thread = threading.Thread(name=f'userdata_thread',
                                                target=self.find_new_traded_symbols,
                                                daemon=True)
        symbol_search_thread.start()

        # userdata_thread = threading.Thread(name=f'userdata_thread', target=self.process_userdata, daemon=True)
        # userdata_thread.start()

        for symbol in self.config.symbols:
            symbol_trade_thread = threading.Thread(
                name=f'trade_thread_{symbol}', target=self.process_trades, args=(symbol,), daemon=True)
            symbol_trade_thread.start()

        sync_balance_thread = threading.Thread(
            name=f'sync_balance_thread', target=self.sync_account, daemon=True)
        sync_balance_thread.start()

        sync_trades_thread = threading.Thread(
            name=f'sync_trades_thread', target=self.sync_trades, daemon=True)
        sync_trades_thread.start()

        sync_orders_thread = threading.Thread(
            name=f'sync_orders_thread', target=self.sync_open_orders, daemon=True)
        sync_orders_thread.start()

    def find_new_traded_symbols(self):
        while True:
            try:
                counter = 0
                for item in self.exchange_information['symbols']:
                    if item['status'] != 'TRADING':
                        continue  # for performance reasons
                    symbol = item['symbol']
                    if symbol not in self.repository.get_symbol_checks():
                        if not self.repository.is_symbol_traded(symbol) and counter < 3:
                            trades = self.rest_manager.get_my_trades(**{'limit': 1, 'symbol': symbol})
                            counter += 1
                            self.repository.process_symbol_checked(symbol)
                            if len(trades) > 0:
                                logger.info(f'Trades found for {symbol}, adding to sync list')
                                self.repository.process_traded_symbol(symbol)
            except Exception as e:
                logger.error(f'Failed to verify unchecked symbols: {e}')

            logger.info('Updated new traded symbols')

            # TODO: once in a while the checked symbols that are not in the DB should be checked
            time.sleep(20)

    def get_asset(self, symbol: str) -> str:
        symbol_informations = self.exchange_information['symbols']
        for symbol_information in symbol_informations:
            if symbol_information['symbol'] == symbol:
                return symbol_information['baseAsset']
        raise Exception(f'No asset found for symbol {symbol}')

    def sync_trades(self):
        first_trade_reached = {}  # key: symbol, value: bool
        max_downloads = 10
        while True:
            try:
                iteration_symbols = []
                counter = 0
                while counter < max_downloads:
                    # TODO: sync symbol of open position first if it was more than 5 minutes ago
                    symbol = self.repository.get_next_traded_symbol()
                    if symbol is not None:
                        self.repository.update_trades_last_downloaded(symbol)
                    if symbol is None or symbol in iteration_symbols:
                        counter += 1
                        continue
                    iteration_symbols.append(symbol)
                    if symbol not in first_trade_reached:
                        first_trade_reached[symbol] = False
                    while first_trade_reached[symbol] is False and counter < max_downloads:
                        counter += 1
                        oldest_trade = self.repository.get_oldest_trade(symbol)
                        if oldest_trade is None:
                            # API will return inclusive, don't want to return the oldest record again
                            oldest_timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
                        else:
                            oldest_timestamp = oldest_trade.timestamp
                            logger.warning(f'Synced trades before {oldest_timestamp} for {symbol}')

                        exchange_trades = self.rest_manager.get_my_trades(**{'symbol': symbol, 'limit': 1000,
                                                                             'endTime': oldest_timestamp - 1})
                        logger.info(f"Length of older trades fetched up to {oldest_timestamp}: {len(exchange_trades)} for {symbol}")
                        trades = []
                        for exchange_trade in exchange_trades:
                            trade = Trade(symbol=exchange_trade['symbol'],
                                          asset=self.get_asset(exchange_trade['symbol']),
                                          order_id=exchange_trade['orderId'],
                                          quantity=exchange_trade['qty'],
                                          price=exchange_trade['price'],
                                          type='REALIZED_PNL',
                                          side='BUY' if exchange_trade['isBuyer'] is True else 'SELL',
                                          timestamp=int(exchange_trade['time']))
                            trades.append(trade)
                        self.repository.process_trades(trades)
                        if len(exchange_trades) < 1:
                            first_trade_reached[symbol] = True

                    # WARNING: don't use forward-walking only, because binance only returns max 7 days when using forward-walking
                    # If this logic is ever changed, make sure that it's still able to retrieve all the account history
                    newest_trade_reached = False
                    while newest_trade_reached is False and counter < max_downloads:
                        counter += 1
                        newest_trade = self.repository.get_newest_trade(symbol)
                        if newest_trade is None:
                            # Binance started in September 2017, so no trade can be before that
                            # newest_timestamp = int(datetime.datetime.fromisoformat('2017-09-01 00:00:00+00:00').timestamp() * 1000)
                            newest_order_id = 0
                        else:
                            # newest_timestamp = newest_trade.timestamp
                            newest_order_id = newest_trade.order_id
                            # logger.warning(f'Synced newer trades since {newest_timestamp}')
                            logger.warning(f'Synced newer trades since {newest_order_id}')

                        exchange_trades = self.rest_manager.get_my_trades(**{'symbol': symbol,
                                                                             # 'limit': 1000,
                                                                             'orderId': newest_order_id + 1})
                                                                             # 'startTime': newest_timestamp + 1})
                        logger.info(f"Length of newer trades fetched from id {newest_order_id}: {len(exchange_trades)} for {symbol}")
                        trades = []
                        for exchange_trade in exchange_trades:
                            trade = Trade(symbol=exchange_trade['symbol'],
                                          asset=self.get_asset(exchange_trade['symbol']),
                                          order_id=exchange_trade['orderId'],
                                          quantity=exchange_trade['qty'],
                                          price=exchange_trade['price'],
                                          type='REALIZED_PNL',
                                          side='BUY' if exchange_trade['isBuyer'] is True else 'SELL',
                                          timestamp=int(exchange_trade['time']))
                            trades.append(trade)
                        self.repository.process_trades(trades)
                        if len(exchange_trades) < 1:
                            newest_trade_reached = True

                    if newest_trade_reached:  # all trades downloaded
                        # calculate incomes
                        incomes = self.calculate_incomes(symbol=symbol, trades=self.repository.get_trades(symbol))
                        self.repository.process_incomes(incomes)
                logger.warning('Synced trades')
            except Exception as e:
                logger.error(f'Failed to process trades: {e}')

            time.sleep(60)

    def calc_long_pprice(self, long_psize, trades: List[TradeEntity]):
        trades.sort(key=lambda x: x.timestamp)
        psize, pprice = 0.0, 0.0
        for trade in trades:
            abs_qty = abs(trade.quantity)
            if trade.side == 'BUY':
                new_psize = psize + abs_qty
                pprice = pprice * (psize / new_psize) + trade.price * (abs_qty / new_psize)
                psize = new_psize
            else:
                psize = max(0.0, psize - abs_qty)
        return pprice

    def calc_long_pnl(self, entry_price, close_price, qty, inverse, c_mult) -> float:
        if inverse:
            if entry_price == 0.0 or close_price == 0.0:
                return 0.0
            return abs(qty) * c_mult * (1.0 / entry_price - 1.0 / close_price)
        else:
            return abs(qty) * (close_price - entry_price)

    def calculate_incomes(self, symbol: str, trades: List[TradeEntity]) -> List[Income]:
        incomes = []
        psize, pprice = 0.0, 0.0
        for trade in trades:
            if trade.side == 'BUY':
                new_psize = psize + trade.quantity
                pprice = pprice * (psize / new_psize) + trade.price * (trade.quantity / new_psize)
                psize = new_psize
            elif psize > 0.0:
                income = Income(symbol=symbol,
                                asset=trade.asset,
                                type='REALIZED_PNL',
                                income=self.calc_long_pnl(pprice, trade.price, trade.quantity, False, 1.0),
                                timestamp=trade.timestamp,
                                transaction_id=trade.order_id)
                incomes.append(income)
                psize = max(0.0, psize - trade.quantity)
        return incomes

    def sync_account(self):
        while True:
            try:
                account = self.rest_manager.get_account()
                current_prices = self.rest_manager.get_all_tickers()
                total_usdt_wallet_balance = 0.0
                total_unrealized_profit = 0.0
                asset_balances = []
                positions = []
                for balance in account['balances']:
                    asset = balance['asset']
                    free = float(balance['free'])
                    locked = float(balance['locked'])
                    position_size = free + locked
                    if position_size > 0.0:
                        if asset in ['USDT', 'BUSD', 'USD']:
                            total_usdt_wallet_balance += position_size
                        else:
                            current_usd_prices = [p for p in current_prices if p['symbol'] in [f'{asset}USDT', f'{asset}BUSD', f'{asset}USD']]
                            if len(current_usd_prices) > 0:
                                asset_usd_balance = 0.0
                                unrealized_profit = 0.0
                                for current_usd_price in current_usd_prices:
                                    symbol = current_usd_price['symbol']
                                    symbol_trades = self.repository.get_trades_by_asset(symbol)
                                    # little hack: assume no position if no orders open
                                    if len(symbol_trades) > 0 and len(self.repository.get_open_orders(symbol)) > 0:
                                        position_price = self.calc_long_pprice(long_psize=position_size, trades=symbol_trades)

                                        asset_usd_balance = position_size * position_price
                                        # position size is already bigger than 0, so there is a position
                                        unrealized_profit = (self.get_current_price(symbol) - position_price) * position_size
                                        total_unrealized_profit += unrealized_profit

                                        position = Position(symbol=symbol,
                                                            entry_price=position_price,
                                                            position_size=position_size,
                                                            side='LONG',
                                                            unrealizedProfit=unrealized_profit,
                                                            initial_margin=0.0)
                                        positions.append(position)
                                asset_balance = AssetBalance(asset=balance['asset'],
                                                             balance=asset_usd_balance,
                                                             unrealizedProfit=unrealized_profit)
                                asset_balances.append(asset_balance)
                            else:
                                logger.warning(f'NO PRICE FOUND FOR USDT FOR SYMBOL {asset}')

                coin_usdt_balance = sum([b.balance for b in asset_balances])
                total_usdt_wallet_balance += coin_usdt_balance
                logger.info(f"Total wallet balance in USDT = {total_usdt_wallet_balance}")

                total_balance = Balance(totalBalance=total_usdt_wallet_balance,
                                        totalUnrealizedProfit=total_unrealized_profit,
                                        assets=asset_balances)

                self.repository.process_balances(total_balance)
                self.repository.process_positions(positions)
                logger.warning('Synced account')
            except Exception as e:
                logger.error(f'Failed to process balance: {e}')

            time.sleep(20)

    def sync_open_orders(self):
        while True:
            orders = []
            try:
                open_orders = self.rest_manager.get_open_orders()
                for open_order in open_orders:
                    order = Order()
                    order.symbol = open_order['symbol']
                    order.price = float(open_order['price'])
                    order.quantity = float(open_order['origQty'])
                    order.side = open_order['side']
                    order.position_side = 'LONG'
                    order.type = open_order['type']
                    orders.append(order)
            except Exception as e:
                logger.error(f'Failed to process open orders for symbol: {e}')
            self.repository.process_orders(orders)

            logger.warning('Synced orders')

            time.sleep(30)

    def get_current_price(self, symbol: str) -> float:
        if symbol not in self.tick_symbols:
            symbol_trade_thread = threading.Thread(
                name=f'trade_thread_{symbol}', target=self.process_trades, args=(symbol,), daemon=True)
            symbol_trade_thread.start()

        curr_price = self.repository.get_current_price(symbol)
        return curr_price.price if curr_price else 0.0

    def process_trades(self, symbol: str):
        if symbol in self.tick_symbols:
            logger.error(f'Already listening to ticks for {symbol}, not starting new processing!')
            return
        self.tick_symbols.append(symbol)
        # stream buffer is set to length 1, because we're only interested in the most recent tick
        self.ws_manager.create_stream(channels=['aggTrade'],
                                      markets=symbol,
                                      stream_buffer_name=f"trades_{symbol}",
                                      output="UnicornFy",
                                      stream_buffer_maxlen=1)
        logger.info(f"Trade stream started for {symbol}")
        while True:
            if self.ws_manager.is_manager_stopping():
                logger.debug('Stopping trade-stream processing...')
                break
            event = self.ws_manager.pop_stream_data_from_stream_buffer(
                stream_buffer_name=f"trades_{symbol}")
            if event and 'event_type' in event and event['event_type'] == 'aggTrade':
                logger.debug(event)
                tick = Tick(symbol=event['symbol'],
                            price=float(event['price']),
                            qty=float(event['quantity']),
                            timestamp=int(event['trade_time']))
                logger.debug(f"Processed tick for {tick.symbol}")
                self.repository.process_tick(tick)
            # Price update every 5 seconds is fast enough
            time.sleep(5)
        logger.warning('Stopped trade-stream processing')
        self.tick_symbols.remove(symbol)