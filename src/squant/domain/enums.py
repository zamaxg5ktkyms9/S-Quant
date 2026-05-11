from enum import StrEnum


class SystemState(StrEnum):
    IDLE = "IDLE"
    SIGNAL_SENT = "SIGNAL_SENT"          # signal issued, awaiting operator confirmation
    HOLDING = "HOLDING"
    SETTLING = "SETTLING"                # sold, T+2 funds locked


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_STOP = "TIME_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL = "MANUAL"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
