"""Consume isolated real model publications into isolated paused trader ledgers."""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

from longtime.config import Settings
from longtime.service import App

async def main():
    base = Path('/root/single-analysis/runtime/audit-2026-09-16')
    name = Path.cwd().name
    app = App(Settings(_env_file=None, runtime_dir=base/name, signal_db=base/'signals.db'),
              exchange=AsyncMock(), markets=AsyncMock())
    app.store.set('entries_paused', True)
    deadline = time.monotonic()+180
    while time.monotonic()<deadline:
        await app.consumer.tick()
        with sqlite3.connect(f'file:{base}/analysis.db?mode=ro',uri=True) as db:
            running = db.execute("SELECT count(*) FROM cycles WHERE status='RUNNING'").fetchone()[0]
        if not running:
            break
        await asyncio.sleep(1)
    assert not app.exchange.method_calls, 'Unexpected exchange access'
    rows=app.store.rows('SELECT signal_id,symbol,status FROM signals')
    assert all(r['status'] in ('SKIP_MODEL','SKIP_PAUSED','SKIP_NOT_NORMAL_CANDIDATE') for r in rows)
    output={'system':name,'receipts':rows,'exchange_calls':0,'isolated':True}
    (base/(name+'-receipts.json')).write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps(output,ensure_ascii=False))

asyncio.run(main())
