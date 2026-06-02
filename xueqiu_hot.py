"""
雪球飙升榜数据获取脚本
API: /v5/stock/hot_stock/new_list.json?type=10&order_by=rank_change
"""

import time
import requests
from datetime import datetime


def fetch_biaosheng(page=1, size=100):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': 'https://xueqiu.com/',
    }
    session = requests.Session()
    session.headers.update(headers)
    session.get('https://xueqiu.com/hq', timeout=15)

    ts = int(time.time() * 1000)
    url = (f'https://stock.xueqiu.com/v5/stock/hot_stock/new_list.json'
           f'?page={page}&size={size}&order=desc&order_by=rank_change&type=10&_={ts}')
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def display_data(data):
    items = data.get('data', {}).get('items', [])
    if not items:
        print('没有获取到数据')
        return

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'\n{"="*90}')
    print(f'  雪球飙升榜  ({now})')
    print(f'{"="*90}')
    print(f'{"排名":>4} {"名称":<14} {"代码":<14} {"现价":>10} {"涨跌幅":>9} '
          f'{"涨跌额":>9} {"热度值":>8} {"排名变化":>8}')
    print(f'{"-"*90}')

    for i, item in enumerate(items, 1):
        name = item.get('name', '')
        symbol = item.get('symbol', '')
        current = item.get('current')
        percent = item.get('percent')
        chg = item.get('chg')
        value = item.get('value', 0)
        rank_change = item.get('rank_change', '')

        cur = f'{current:.2f}' if current is not None else 'N/A'
        pct = f'{percent:+.2f}%' if percent is not None else 'N/A'
        cg = f'{chg:+.2f}' if chg is not None else 'N/A'
        val = f'{value:.0f}' if value else 'N/A'
        rc = f'{rank_change:+d}' if isinstance(rank_change, (int, float)) and rank_change != 0 else '—'

        print(f'{i:>4} {name:<14} {symbol:<14} {cur:>10} {pct:>9} '
              f'{cg:>9} {val:>8} {rc:>8}')

    print(f'{"="*90}')
    print(f'共 {len(items)} 条记录\n')


def main():
    try:
        data = fetch_biaosheng()
        display_data(data)
    except Exception as e:
        print(f'出错: {e}')


if __name__ == '__main__':
    main()
