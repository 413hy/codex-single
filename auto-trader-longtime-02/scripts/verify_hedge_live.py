"""Read-only exchange evidence for the independent hedge Demo deployment."""
import asyncio
import json
import time

from longtime.config import DEMO_URL, Settings
from longtime.exchange import Exchange
from longtime.risk import number
from longtime.store import Store


async def verify():
    settings = Settings()
    settings.require_credentials()
    settings = settings.model_copy(update={'trading_enabled': False})
    exchange = Exchange(settings)
    store = Store(settings.runtime_dir/'trader.db')
    try:
        await exchange.sync_clock()
        account = await exchange.account()
        positions = await exchange.active_positions()
        orders = await exchange.open_orders()
        groups = [json.loads(r['document']) for r in store.rows('SELECT document FROM hedge_groups')]
        checks = []
        for g in groups:
            related = [o for o in orders if o['symbol'] == g['symbol']]
            slots = [p for p in positions if p['symbol'] == g['symbol']]
            check = dict(group_id=g['group_id'], symbol=g['symbol'], phase=g['phase'],
                         positions=len(slots), orders=len(related), errors=[])
            if any(number(p.get('stopLoss') or 0) != 0 for p in slots):
                check['errors'].append('unexpected attached stop loss')
            if g['phase'] == 'SINGLE':
                active = store.trade(g['active'])
                child = store.trade(g['child'])
                tp = next((o for o in related if o['orderId'] == active['tp_order_id']), None)
                hedge = await exchange.order(g['symbol'], json.loads(child['details'])['entry_link'])
                if not tp or not hedge:
                    check['errors'].append('TP or hedge order not present; reconcile transition')
                else:
                    if tp.get('reduceOnly') is not True or tp['orderType'] != 'Limit':
                        check['errors'].append('TP is not reduce-only limit')
                    if hedge.get('reduceOnly') is not False or hedge['orderType'] != 'Limit':
                        check['errors'].append('hedge is not opening limit')
                    if int(hedge['positionIdx']) != 3-int(tp['positionIdx']):
                        check['errors'].append('hedge position index mismatch')
                    if number(hedge['qty']) != number(child['qty']):
                        check['errors'].append('hedge quantity mismatch')
                    if number(hedge['price']) != number(active['sl_price']):
                        check['errors'].append('hedge price mismatch')
                    if hedge['orderStatus'] == 'Untriggered' and number(hedge['triggerPrice']) != number(hedge['price']):
                        check['errors'].append('hedge trigger/limit mismatch')
                    check['tp'] = {k:tp.get(k) for k in ('orderId','price','qty','positionIdx','reduceOnly')}
                    check['hedge'] = {k:hedge.get(k) for k in ('orderId','price','triggerPrice','qty','cumExecQty','positionIdx','reduceOnly','orderStatus')}
            if g['phase'] == 'LOCKED' and (len(slots) != 2 or related):
                check['errors'].append('locked state must have two legs and no orders')
            checks.append(check)
        result = dict(at=time.time(), endpoint=DEMO_URL, read_only=True, account=account,
                      positions=[{k:p.get(k) for k in ('symbol','side','size','avgPrice','positionIdx')} for p in positions],
                      order_count=len(orders), checks=checks,
                      incidents=store.rows("SELECT scope,error FROM incidents WHERE status='OPEN'"),
                      notifications=store.rows('SELECT status,count(*) n FROM outbox GROUP BY status'))
        path = settings.runtime_dir/'live-depth-check.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await exchange.close()


if __name__ == '__main__':
    asyncio.run(verify())
