"""Integration across separately imported live source trees using isolated databases."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from analysis_core.model import Decision
from analysis_core.signals import HedgeSniffer, SignalBus


async def test_real_publisher_both_consumers_and_hedge_snapshot(tmp_path):
    roots = [Path('/root/auto-trader-longtime'), Path('/root/auto-trader-longtime-02')]
    if not all((root / '.venv/bin/python').exists() for root in roots):
        pytest.skip('Sibling trading source trees are required for cross-system audit')
    bus = SignalBus(tmp_path/'signals.db')
    for sid, side, normal, now in [
        ('ordinary', 'LONG', True, time.time()),
        ('skip', 'SKIP', True, time.time()),
        ('extra', 'SHORT', False, time.time()),
        ('expired', 'LONG', True, time.time()-61),
    ]:
        bus.publish(sid, Decision(symbol='TESTUSDT',decision=side,reason='isolated integration fixture'),
                    normal=normal,hedge=None,observed_at='fixture',now=now)
    worker = Path(__file__).parents[1]/'docs/audit-2026-09-16/cross_system_worker.py'
    for root in roots:
        proc = await asyncio.create_subprocess_exec(
            str(root/'.venv/bin/python'),str(worker),str(root),str(tmp_path),cwd=root,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
        assert proc.returncode == 0, stderr.decode()
        assert json.loads(stdout)['restart_no_replay']
    sniffer = HedgeSniffer(tmp_path/'auto-trader-longtime-02/trader.db')
    snapshot = sniffer.sniff()
    assert 'TESTUSDT' in snapshot and len(snapshot['TESTUSDT']['positions']) == 2
    with pytest.raises(RuntimeError):
        sniffer.sniff(time.time()+21)
