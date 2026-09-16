"""Isolated real consumer/executor/ledger; only exchange and markets are fixtures."""
import asyncio
import json
import sys
from pathlib import Path

root, workspace = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / 'tests'))
from conftest import FakeExchange, FakeMarkets
from longtime.config import Settings
from longtime.service import App

async def main():
    hedge = root.name.endswith('-02')
    if hedge:
        from test_hedge_strategy import HedgeExchange
        exchange = HedgeExchange()
    else:
        exchange = FakeExchange()
    config = Settings(_env_file=None, runtime_dir=workspace/root.name,
                      signal_db=workspace/'signals.db', trading_enabled=True)
    app = App(config, exchange=exchange, markets=FakeMarkets())
    await app.consumer.tick()
    receipts = {r['signal_id']:r['status'] for r in app.store.rows('SELECT * FROM signals')}
    assert receipts['feed:ordinary'] == 'OPEN', receipts
    assert receipts['feed:skip'] == 'SKIP_MODEL'
    assert receipts['feed:extra'] == 'SKIP_NOT_NORMAL_CANDIDATE'
    assert 'feed:expired' not in receipts
    assert len(app.store.rows('SELECT * FROM orders')) == 3
    if hedge:
        assert not app.store.rows("SELECT * FROM orders WHERE kind='SL'")
    else:
        assert len(app.store.rows("SELECT * FROM orders WHERE kind='SL'")) == 1
    count = len(exchange.submissions)
    await app.consumer.tick()
    restored = App(config, exchange=exchange, markets=FakeMarkets())
    await restored.consumer.tick()
    assert len(exchange.submissions) == count
    if hedge:
        group = app.executor.hedge.groups()[0]
        child = app.store.trade(group['child'])
        link = json.loads(child['details'])['entry_link']
        exchange.fill(link, exchange.orders[link]['qty'])
        await app.monitor.tick()
        group = app.executor.hedge.get(group['group_id'])
        assert group['phase'] == 'LOCKED'
        sys.path.insert(0, '/root/single-analysis/src')
        from analysis_core.signals import HedgeSniffer, SignalBus
        from analysis_core.model import Decision
        snapshot = HedgeSniffer(app.store.path).sniff()
        ownership = snapshot['TESTUSDT']
        bus = SignalBus(config.signal_db)
        app.store.set('entries_paused', True)
        bus.publish('review', Decision(symbol='TESTUSDT', decision='SHORT', reason='fixture review'),
                    normal=False, hedge={k:ownership[k] for k in ('group_id','generation')},
                    observed_at='fixture')
        await app.consumer.tick()
        assert app.store.rows("SELECT status FROM signals WHERE signal_id='feed:review'")[0]['status'] == 'HEDGE_DIRECTION_APPLIED'
        group = app.executor.hedge.get(group['group_id'])
        assert group['generation'] == 1 and group['phase'] == 'SINGLE'
        count_after_review = len(exchange.submissions)
        await App(config, exchange=exchange, markets=FakeMarkets()).consumer.tick()
        assert len(exchange.submissions) == count_after_review
        child = app.store.trade(group['child'])
        link = json.loads(child['details'])['entry_link']
        exchange.fill(link, exchange.orders[link]['qty'])
        await app.monitor.tick()
        group = app.executor.hedge.get(group['group_id'])
        assert group['phase'] == 'LOCKED'
        bus.publish('stale-generation', Decision(symbol='TESTUSDT', decision='LONG', reason='fixture stale generation'),
                    normal=False, hedge={k:ownership[k] for k in ('group_id','generation')},
                    observed_at='fixture')
        count_stale = len(exchange.submissions)
        await app.consumer.tick()
        assert len(exchange.submissions) == count_stale
        assert not app.store.rows("SELECT * FROM orders WHERE kind='SL'")
    print(json.dumps({'system':root.name,'receipts':receipts,'orders':count,'restart_no_replay':True}))

asyncio.run(main())
