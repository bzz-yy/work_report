"""Resolve explicit and relative months without silently overriding a request."""
import datetime as dt
import re


def monthly_period(request, supplied=None, today=None):
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    dates = set()
    for year, month in re.findall(r'(20\d{2})\s*(?:年|[-/])\s*(\d{1,2})(?!\d)', request):
        if not 1 <= int(month) <= 12:
            raise ValueError('月份必须在1至12之间')
        dates.add(f'{year}-{int(month):02}')
    terms = re.findall(r'上上个?月|上个月|上月|本月|这个月|当月', request)
    for term in terms:
        offset = -2 if term.startswith('上上') else -1 if term.startswith('上') else 0
        index = today.year * 12 + today.month - 1 + offset
        year, month = divmod(index, 12)
        dates.add(f'{year}-{month+1:02}')
    if len(dates) > 1:
        raise ValueError('请求包含多个或相互冲突的报告月份')
    if supplied and dates and supplied not in dates:
        raise ValueError('月份参数与自然语言冲突')
    period = supplied or next(iter(dates), None)
    if not period or not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', period):
        raise ValueError('需要明确年月或上月、本月等相对月份')
    return period
