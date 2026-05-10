from datetime import date, datetime

from squant.utils.jst import now_jst, today_jst


class SystemClock:
    def now_jst(self) -> datetime:
        return now_jst()

    def today_jst(self) -> date:
        return today_jst()
