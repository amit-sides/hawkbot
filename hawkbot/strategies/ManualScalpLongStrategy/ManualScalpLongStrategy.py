import logging
import os
import signal

from pynput import keyboard

from hawkbot.core.model import Position, SymbolInformation, LimitOrder, OrderTypeIdentifier, TimeInForce, Side
from hawkbot.strategies.abstract_base_strategy import AbstractBaseStrategy
from hawkbot.utils import calc_min_qty, round_, fill_required_parameters

logger = logging.getLogger(__name__)


class ManualScalpLongStrategy(AbstractBaseStrategy):
    def __init__(self):
        super().__init__()
        self.entry_offset_price_steps: int = None
        self.nr_orders_per_grid: int = None
        self.grid_width: float = None
        self.initial_entry_size: float = None
        self.dca_multiplier: float = None
        self.listener: keyboard.Listener = keyboard.Listener(on_press=self.on_press)
        self.key_buy: str = None
        self.key_cancel_entry: str = None
        self.key_cancel_all: str = None
        self.key_toggle_tp: str = None
        self.key_toggle_stoploss: str = None
        self.key_close_position: str = None
        self.ctrlc = str(chr(ord("C") - 64))
        self.tp_distances: dict = {}
        self.default_minimum_tp: float = None

        self.key_mapping = {}

    def init(self):
        super().init()
        self.order_executor.exchange.ping()

        required_parameters = ['initial_entry_size',
                               'dca_multiplier',
                               'grid_width',
                               'nr_orders_per_grid',
                               'entry_offset_price_steps',
                               'key_buy',
                               'key_cancel_entry',
                               'key_cancel_all',
                               'key_toggle_tp',
                               'key_toggle_stoploss',
                               'key_close_position']
        fill_required_parameters(target=self, config=self.strategy_config, required_parameters=required_parameters)
        self.default_minimum_tp = self.tp_config.minimum_tp
        self.key_mapping[self.key_buy] = self._place_entry_grid
        self.key_mapping[self.key_cancel_entry] = self._cancel_entry_orders
        self.key_mapping[self.key_cancel_all] = self._cancel_all_orders
        self.key_mapping[self.key_toggle_tp] = self._toggle_tp
        self.key_mapping[self.key_toggle_stoploss] = self._toggle_sl
        self.key_mapping[self.key_close_position] = self._close_position

        if "tp_distances" in self.strategy_config:
            for key, value in self.strategy_config['tp_distances'].items():
                self.tp_distances[key] = value
                self.key_mapping[key] = self._set_tp_distance

        self.tp_config.allow_move_away = True

        self.listener.start()

    def on_press(self, key):
        if not hasattr(key, 'char'):
            return

        if key.char == 'q':
            os.kill(os.getpid(), signal.SIGTERM)

        try:
            key_pressed = key.char
            logger.info(f'{self.symbol} {self.position_side.name}: Key(s) {key_pressed} pressed')
            if key_pressed in self.key_mapping:
                self.key_mapping[key_pressed](key_pressed)
        except:
            logger.exception(f'Error handling key press {key}')

    def _set_tp_distance(self, key_pressed):
        new_tp_distance = self.tp_distances[key_pressed]
        logger.info(f'{self.symbol} {self.position_side.name}: Setting TP distance to {new_tp_distance}')
        self.tp_config.minimum_tp = new_tp_distance
        position = self.exchange_state.position(symbol=self.symbol, position_side=self.position_side)
        symbol_information = self.exchange_state.get_symbol_information(self.symbol)
        current_price = self.exchange_state.get_last_price(self.symbol)
        self.enforce_tp_grid(symbol=self.symbol,
                             position=position,
                             symbol_information=symbol_information,
                             current_price=current_price,
                             wiggle_config=self.wiggle_config)

    def _place_entry_grid(self, key_pressed):
        current_price = self.exchange_state.get_last_price(self.symbol)
        symbol_information = self.exchange_state.get_symbol_information(self.symbol)
        wallet_balance = self.exchange_state.symbol_balance(self.symbol)

        logger.info(f'{self.symbol} {self.position_side.name}: Placing entry grid, starting from price = {current_price}')
        offset = self.entry_offset_price_steps * symbol_information.price_step

        if self.exchange_state.has_no_open_position(symbol=self.symbol, position_side=self.position_side):
            self.order_executor.cancel_orders(self.exchange_state.open_stoploss_orders(symbol=self.symbol, position_side=self.position_side))

        orders = []
        sum_cost = 0.0

        exposed_balance = wallet_balance * self.calc_wallet_exposure_ratio()
        position = self.exchange_state.position(symbol=self.symbol, position_side=self.position_side)
        if position.no_position():
            grid_cost = exposed_balance * self.initial_entry_size
        else:
            grid_cost = position.cost * self.dca_multiplier
        first_order_price = current_price - offset
        cost_per_order = grid_cost / self.nr_orders_per_grid
        order_quantity = round_(cost_per_order / first_order_price, symbol_information.quantity_step)
        grid_order_spacing = self.grid_width / self.nr_orders_per_grid

        for i in range(self.nr_orders_per_grid):
            price = round_(number=first_order_price * (1 - (i * grid_order_spacing)),
                           step=symbol_information.price_step)

            min_quantity = calc_min_qty(price=price,
                                        inverse=False,
                                        qty_step=symbol_information.quantity_step,
                                        min_qty=symbol_information.minimum_quantity,
                                        min_cost=symbol_information.minimal_buy_cost)
            quantity = max(order_quantity, min_quantity)

            cost = price * quantity
            if sum_cost + cost >= exposed_balance:
                break
            else:
                sum_cost += cost

                if self.exchange_state.has_no_open_position(symbol=self.symbol, position_side=self.position_side) and i == 0:
                    order_type_identifier = OrderTypeIdentifier.INITIAL_ENTRY
                else:
                    order_type_identifier = OrderTypeIdentifier.ENTRY

                orders.append(LimitOrder(order_type_identifier=order_type_identifier,
                                         symbol=self.symbol,
                                         quantity=quantity,
                                         side=self.position_side.increase_side(),
                                         position_side=self.position_side,
                                         price=price,
                                         reduce_only=False))

        open_entry_orders = self.exchange_state.open_entry_orders(symbol=self.symbol, position_side=self.position_side)
        self.enforce_grid(new_orders=orders, exchange_orders=open_entry_orders, lowest_price_first=True)

    def _cancel_entry_orders(self, key_pressed):
        symbol = self.symbol
        position_side = self.position_side

        logger.info(f'{symbol} {position_side.name}: Cancelling all open orders')
        if self.exchange_state.has_no_open_position(symbol=self.symbol, position_side=self.position_side):
            self.order_executor.cancel_orders(self.exchange_state.open_stoploss_orders(symbol=self.symbol, position_side=self.position_side))
            trailing_tp_order = self.exchange_state.open_trailing_tp_order(symbol=self.symbol, position_side=self.position_side)
            if trailing_tp_order is not None:
                self.order_executor.cancel_order(trailing_tp_order)
        open_orders = self.exchange_state.open_entry_orders(symbol=symbol, position_side=self.position_side)
        self.order_executor.cancel_orders(open_orders)

    def _cancel_all_orders(self, key_pressed):
        symbol = self.symbol
        position_side = self.position_side

        logger.info(f'{symbol} {position_side.name}: Cancelling all open orders')
        open_orders = self.exchange_state.all_open_orders(symbol=symbol, position_side=self.position_side)
        self.order_executor.cancel_orders(open_orders)

    def _toggle_tp(self, key_pressed):
        if self.tp_config.enabled is True:
            logger.info(f'{self.symbol} {self.position_side.name}: Disabling TP and removing TP orders')
            self.tp_config.enabled = False
            self.order_executor.cancel_orders(self.exchange_state.open_tp_orders(symbol=self.symbol, position_side=self.position_side))
        else:
            logger.info(f'{self.symbol} {self.position_side.name}: Enabling TP and placing TP orders')
            self.tp_config.enabled = True
            position = self.exchange_state.position(symbol=self.symbol, position_side=self.position_side)
            symbol_information = self.exchange_state.get_symbol_information(self.symbol)
            current_price = self.exchange_state.get_last_price(self.symbol)
            self.enforce_tp_grid(symbol=self.symbol,
                                 position=position,
                                 symbol_information=symbol_information,
                                 current_price=current_price,
                                 wiggle_config=self.wiggle_config)

    def _toggle_sl(self, key_pressed):
        if self.stoploss_config.enabled is True:
            logger.info(f'{self.symbol} {self.position_side.name}: Disabling stoploss and removing stoploss orders')
            self.stoploss_config.enabled = False
            self.order_executor.cancel_orders(self.exchange_state.open_stoploss_orders(symbol=self.symbol, position_side=self.position_side))
        else:
            logger.info(f'{self.symbol} {self.position_side.name}: Enabling stoploss and placing stoploss orders')
            self.stoploss_config.enabled = True
            position = self.exchange_state.position(symbol=self.symbol, position_side=self.position_side)
            symbol_information = self.exchange_state.get_symbol_information(self.symbol)
            current_price = self.exchange_state.get_last_price(self.symbol)
            self.enforce_stoploss(symbol=self.symbol,
                                  position=position,
                                  position_side=self.position_side,
                                  symbol_information=symbol_information,
                                  current_price=current_price)

    def _close_position(self, key_pressed):
        position = self.exchange_state.position(symbol=self.symbol, position_side=self.position_side)
        if position.has_position():
            logger.info(f'{self.symbol} {self.position_side.name}: Closing position {position}')
            symbol_information = self.exchange_state.get_symbol_information(self.symbol)
            current_price = self.exchange_state.get_last_price(self.symbol)
            sell_price = round_(number=current_price - (2 * symbol_information.price_step), step=symbol_information.price_step)
            self._cancel_all_orders(key_pressed)
            self.order_executor.create_order(LimitOrder(order_type_identifier=OrderTypeIdentifier.GTFO,
                                                        symbol=self.symbol,
                                                        quantity=position.position_size,
                                                        side=Side.SELL,
                                                        position_side=position.position_side,
                                                        initial_entry=False,
                                                        price=sell_price,
                                                        reduce_only=True,
                                                        time_in_force=TimeInForce.GOOD_TILL_CANCELED))

    def on_tp_order_filled(self,
                           symbol: str,
                           position: Position,
                           symbol_information: SymbolInformation,
                           wallet_balance: float,
                           current_price: float):
        super().on_position_closed(symbol=symbol,
                                   position=position,
                                   symbol_information=symbol_information,
                                   wallet_balance=wallet_balance,
                                   current_price=current_price)
        self.tp_config.minimum_tp = self.default_minimum_tp

    def on_stoploss_filled(self,
                           symbol: str,
                           position: Position,
                           symbol_information: SymbolInformation,
                           wallet_balance: float,
                           current_price: float):
        super().on_position_closed(symbol=symbol,
                                   position=position,
                                   symbol_information=symbol_information,
                                   wallet_balance=wallet_balance,
                                   current_price=current_price)
        self.tp_config.minimum_tp = self.default_minimum_tp

    def on_position_closed(self,
                           symbol: str,
                           position: Position,
                           symbol_information: SymbolInformation,
                           wallet_balance: float,
                           current_price: float):
        super().on_position_closed(symbol=symbol,
                                   position=position,
                                   symbol_information=symbol_information,
                                   wallet_balance=wallet_balance,
                                   current_price=current_price)
        self.tp_config.minimum_tp = self.default_minimum_tp

    def on_shutdown(self,
                    symbol: str,
                    position: Position,
                    symbol_information: SymbolInformation,
                    wallet_balance: float,
                    current_price: float):
        super().on_shutdown(symbol=symbol,
                            position=position,
                            symbol_information=symbol_information,
                            wallet_balance=wallet_balance,
                            current_price=current_price)
        try:
            self.listener.stop()
        except:
            logger.exception(f"{symbol} {self.position_side.name}: Failed to stop keyboard listener")
