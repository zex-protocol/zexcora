from enum import Enum
import heapq
from struct import unpack
from collections import deque
from threading import Lock
from time import time as unix_time
from typing import Optional, Callable

from pydantic import BaseModel
import pandas as pd

from verify import verify, chunkify


class Operation(Enum):
    DEPOSIT = 100
    WITHDRAW = 119
    BUY = 98
    SELL = 115
    CANCEL = 99


TRADES_TTL = 1000


class MarketTransaction(BaseModel):
    version: int
    operation: str
    base_chain: str
    base_token_id: int
    quote_chain: str
    quote_token_id: int
    amount: float
    price: float
    time: int
    nonce: int
    public: bytes
    signature: bytes
    index: int

    raw_tx: Optional[bytes]

    @classmethod
    def from_tx(cls, tx: bytes) -> "MarketTransaction":
        assert len(tx) == 145, f"invalid transaction len(tx): {len(tx)}"
        return MarketTransaction(
            version=tx[0],
            operation=chr(tx[1]),
            base_chain=tx[2:5],
            base_token_id=unpack(">I", tx[5:9])[0],
            quote_chain=tx[9:12],
            quote_token_id=unpack(">I", tx[12:16])[0],
            amount=unpack(">d", tx[16:24])[0],
            price=unpack(">d", tx[24:32])[0],
            time=unpack(">I", tx[32:36])[0],
            nonce=unpack(">I", tx[36:40])[0],
            public=tx[40:73],
            signature=tx[73:137],
            index=unpack(">Q", tx[137:145])[0],
            raw_tx=tx,
        )

    @property
    def pair(self):
        return f"{self.base_chain}:{self.base_token_id}-{self.quote_chain}:{self.quote_token_id}"

    @property
    def base_token(self):
        return f"{self.base_chain}:{self.base_token_id}"

    @property
    def quote_token(self):
        return f"{self.quote_chain}:{self.quote_token_id}"

    @property
    def order_slice(self):
        return self.raw_tx[2:41]

    def hex(self):
        return self.raw_tx.hex()


class Deposit(BaseModel):
    token: int
    amount: float
    time: int
    public: bytes


class DepositTransaction(BaseModel):
    version: int
    operation: str
    chain: str
    from_block: int
    to_block: int
    deposits: list[Deposit]
    signature: bytes

    @classmethod
    def from_tx(cls, tx: bytes) -> "DepositTransaction":
        deposits = []
        deposit_count = unpack(">H", tx[21:23][0])
        for i in range(deposit_count):
            offset = 23 + i * 49
            deposits.append(
                Deposit(
                    token=unpack(">I", tx[offset : offset + 4])[0],
                    amount=unpack(">d", tx[offset + 4 : offset + 8])[0],
                    time=unpack(">I", tx[offset + 8 : offset + 12])[0],
                    public=tx[offset + 12 : offset + 33],
                )
            )

        return DepositTransaction(
            version=tx[0],
            operation=tx[1],
            chain=tx[2:5],
            from_block=unpack(">Q", tx[5:13])[0],
            to_block=unpack(">Q", tx[13:21])[0],
            deposits=deposits,
            signature=tx[-32:],
        )


def parse_tx(tx):
    assert tx[0] == 1, "invalid transaction version"
    if tx[1] in [Operation.BUY, Operation.SELL, Operation.CANCEL]:
        return MarketTransaction.from_tx(tx)
    if tx[1] == Operation.DEPOSIT:
        return DepositTransaction.from_tx(tx)
    raise NotImplementedError(f"transaction operation {tx[1]} is not valid")


class SingletonMeta(type):
    """
    This is a thread-safe implementation of Singleton.
    """

    _instances = {}

    _lock: Lock = Lock()
    """
    We now have a lock object that will be used to synchronize threads during
    first access to the Singleton.
    """

    def __call__(cls, *args, **kwargs):
        """
        Possible changes to the value of the `__init__` argument do not affect
        the returned instance.
        """
        # Now, imagine that the program has just been launched. Since there's no
        # Singleton instance yet, multiple threads can simultaneously pass the
        # previous conditional and reach this point almost at the same time. The
        # first of them will acquire lock and will proceed further, while the
        # rest will wait here.
        with cls._lock:
            # The first thread to acquire the lock, reaches this conditional,
            # goes inside and creates the Singleton instance. Once it leaves the
            # lock block, a thread that might have been waiting for the lock
            # release may then enter this section. But since the Singleton field
            # is already initialized, the thread won't create a new object.
            if cls not in cls._instances:
                instance = super().__call__(*args, **kwargs)
                cls._instances[cls] = instance
        return cls._instances[cls]


class Zex(metaclass=SingletonMeta):
    def __init__(
        self,
        kline_callback: Callable[[pd.DataFrame], None],
        depth_callback: Callable[[dict], None],
    ):
        self.kline_callback = kline_callback
        self.depth_callback = depth_callback

        self.queues: dict[str, TradingQueue] = {}
        self.balances = {}
        self.amounts = {}
        self.trades = {}
        self.orders = {}
        self.deposited_blocks = {"pol": 0, "eth": 0, "bst": 39493054}
        self.nonces = {}

    def process(self, txs: list[bytes]):
        verify(txs)
        print(txs)
        modified_pairs = set()
        for tx in txs:
            if not tx:
                continue
            v, name = tx[0:2]
            if v != 1:
                continue
            try:
                if name == Operation.DEPOSIT.value:
                    self.deposit(tx)
                elif name == Operation.WITHDRAW.value:
                    self.withdraw(tx)
                elif name in (Operation.BUY.value, Operation.SELL.value):
                    tx = MarketTransaction.from_tx(tx)
                    if tx.pair not in self.queues:
                        base, quote = tx.pair.split("-")
                        self.queues[tx.pair] = TradingQueue(base, quote, self)
                    self.queues[tx.pair].place(tx)
                    while self.queues[tx.pair].match(tx.time):
                        pass
                    modified_pairs.add(tx.pair)

                elif name == Operation.CANCEL.value:
                    tx = MarketTransaction.from_tx(tx)
                    self.queues[tx.pair].cancel(tx)
                    modified_pairs.add(tx.pair)
                else:
                    raise ValueError(f"invalid transaction name {name}")
            except Exception as e:
                print(e, tx.hex())
                raise
        for pair in modified_pairs:
            self.kline_callback(self.get_kline(pair))
            self.depth_callback(self.get_order_book(pair))

    def deposit(self, tx: bytes):
        chain = tx[2:5].decode()
        from_block, to_block, count = unpack(">QQH", tx[5:23])
        assert (
            self.deposited_blocks[chain] == from_block - 1
        ), f"invalid from block. self.deposited_blocks[chain]: {self.deposited_blocks[chain]}, from_block - 1: {from_block - 1}"
        self.deposited_blocks[chain] = to_block
        deposits = list(chunkify(tx[23 : 23 + 49 * count], 49))
        for chunk in deposits:
            token = f"{chain}:{unpack('>I', chunk[:4])[0]}"
            amount, t = unpack(">dI", chunk[4:16])
            public = chunk[16:49]
            if token not in self.balances:
                self.balances[token] = {}
            if public not in self.balances[token]:
                self.balances[token][public] = 0
            self.balances[token][public] += amount
            print(self.balances)
            if public not in self.trades:
                self.trades[public] = deque()
                self.orders[public] = {}
                self.nonces[public] = 0

    def cancel(self, tx: MarketTransaction):
        for order in self.orders[tx.public]:
            if tx.order_slice not in order:
                continue
            self.amounts[order] = 0
            if ord(tx.operation) == Operation.BUY:
                self.balances[tx.quote_token][tx.public] += tx.amount * tx.price
            else:
                self.balances[tx.base_token][tx.public] += tx.amount
            break
        else:
            raise Exception("order not found")

    def withdraw(self, tx):
        pass

    def get_order_book(self, pair: str):
        now = int(unix_time() * 1000)
        return {
            "e": "depthUpdate",  # Event type
            "E": now,  # Event time
            "T": now,  # Transaction time
            "s": pair.capitalize(),
            "U": 390497796,
            "u": 390497878,
            "pu": 390497794,
            "b": [  # Bids to be updated
                ["7403.89", "0.002"],  # Price Level to be  # Quantity
                ["7403.90", "3.906"],
                ["7404.00", "1.428"],
                ["7404.85", "5.239"],
                ["7405.43", "2.562"],
            ],
            "a": [  # Asks to be updated
                ["7405.96", "3.340"],  # Price level to be  # Quantity
                ["7406.63", "4.525"],
                ["7407.08", "2.475"],
                ["7407.15", "4.800"],
                ["7407.20", "0.175"],
            ],
        }

    def get_order_book_all(self):
        return [{}]

    def get_kline(self, pair: str) -> pd.DataFrame:
        return self.queues[pair].kline


def get_current_1m_open_time():
    now = int(unix_time())
    open_time = now - now % 60
    return open_time * 1000


class TradingQueue:
    def __init__(self, base_token, quote_token, zex: Zex):
        self.buy_orders = []  # Max heap for buy orders, prices negated
        self.sell_orders = []  # Min heap for sell orders
        heapq.heapify(self.buy_orders)
        heapq.heapify(self.sell_orders)
        self.base_token = base_token
        self.quote_token = quote_token
        self.pair = base_token + quote_token
        self.kline = pd.DataFrame(
            columns=[
                "OpenTime",
                "CloseTime",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
                "NumberOfTrades",
            ],
        ).set_index("OpenTime")

        self.zex = zex

    def place(self, tx: MarketTransaction):
        assert self.zex.nonces[tx.public] == tx.nonce, "invalid nonce"
        if ord(tx.operation) == Operation.BUY.value:
            required = tx.amount * tx.price
            balance = self.zex.balances[tx.quote_token].get(tx.public, 0)
            assert balance >= required, "balance not enough"
            heapq.heappush(self.buy_orders, (-tx.price, tx.index, tx))
            self.zex.balances[tx.quote_token][tx.public] = balance - required
        elif ord(tx.operation) == Operation.SELL.value:
            required = tx.amount
            balance = self.zex.balances[tx.base_token].get(tx.public, 0)
            assert (
                balance >= required
            ), f"balance not enough.\ncurrent balance: {balance}\nbase_token: {tx.base_token}\ntx: {tx}\nall balances:{self.zex.balances}"
            heapq.heappush(self.sell_orders, (tx.price, tx.index, tx))
            self.zex.balances[tx.base_token][tx.public] = balance - required
        else:
            raise NotImplementedError(
                f"transaction type {tx.operation} is not supported"
            )
        self.zex.amounts[tx.raw_tx] = tx.amount
        self.zex.orders[tx.public][tx.raw_tx] = True
        self.zex.nonces[tx.public] += 1

    def match(self, t):
        # Match orders while there are matching orders available
        if not self.buy_orders or not self.sell_orders:
            return False

        # Check if the top buy order matches the top sell order
        buy_price, buy_i, buy_order = self.buy_orders[0]
        sell_price, sell_i, sell_order = self.sell_orders[0]
        buy_price = -buy_price
        if buy_price < sell_price:
            return False

        # Determine the amount to trade
        trade_amount = min(
            self.zex.amounts[buy_order.raw_tx], self.zex.amounts[sell_order.raw_tx]
        )

        # Update orders
        buy_public = buy_order.public
        sell_public = sell_order.public

        if self.zex.amounts[buy_order.raw_tx] > trade_amount:
            self.zex.amounts[buy_order.raw_tx] -= trade_amount
        else:
            heapq.heappop(self.buy_orders)
            del self.zex.amounts[buy_order.raw_tx]
            del self.zex.orders[buy_public][buy_order.raw_tx]

        if self.zex.amounts[sell_order.raw_tx] > trade_amount:
            self.zex.amounts[sell_order.raw_tx] -= trade_amount
        else:
            heapq.heappop(self.sell_orders)
            del self.zex.amounts[sell_order.raw_tx]
            del self.zex.orders[sell_public][sell_order.raw_tx]

        current_candle_index = get_current_1m_open_time()
        if current_candle_index in self.kline.index:
            self.kline.loc[current_candle_index, "High"] = max(
                sell_price, self.kline.loc[current_candle_index, "High"]
            )
            self.kline.loc[current_candle_index, "Low"] = min(
                sell_price, self.kline.loc[current_candle_index, "Low"]
            )
            self.kline.loc[current_candle_index, "Close"] = sell_price
            self.kline.loc[current_candle_index, "Volume"] = (
                self.kline.loc[current_candle_index, "Volume"] + trade_amount
            )
            self.kline.loc[current_candle_index, "NumberOfTrades"] += 1
        else:
            print("########################## else block ######################")
            self.kline.loc[current_candle_index] = [
                current_candle_index + 59999,  # CloseTime
                sell_price,  # Open
                sell_price,  # High
                sell_price,  # Low
                sell_price,  # Close
                trade_amount,  # Volume
                1,  # NumberOfTrades
            ]

        # Amount for canceled order is set to 0
        if trade_amount == 0:
            return False

        # Update users balances
        price = sell_price if sell_i < buy_i else buy_price
        base_balance = self.zex.balances[self.base_token].get(buy_public, 0)
        self.zex.balances[self.base_token][buy_public] = base_balance + trade_amount
        quote_balance = self.zex.balances[self.quote_token].get(sell_public, 0)
        self.zex.balances[self.quote_token][sell_public] = (
            quote_balance + price * trade_amount
        )

        # Add trades to users in-memory history
        buy_q = self.zex.trades[buy_public]
        sell_q = self.zex.trades[sell_public]
        buy_q.append((t, trade_amount, self.pair, Operation.BUY))
        sell_q.append((t, trade_amount, self.pair, Operation.SELL))

        # Remove trades older than TRADES_TTL from users in-memory history
        while len(buy_q) > 0 and t - buy_q[0][0] > TRADES_TTL:
            buy_q.popleft()
        while len(sell_q) > 0 and t - sell_q[0][0] > TRADES_TTL:
            sell_q.popleft()

        return True
