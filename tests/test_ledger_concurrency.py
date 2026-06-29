"""Ledger thread-safety: concurrent writes lose nothing and keep totals exact."""

import threading
from sovereign_os.ledger.unified_ledger import UnifiedLedger


def test_concurrent_writes_are_exact():
    led = UnifiedLedger()
    N_THREADS, PER = 12, 200

    def worker(k):
        for i in range(PER):
            led.record_usd(1, purpose=f"t{k}")          # +1 cent each
            led.record_token("gpt-4o", 10, 5, estimated_usd_cents=1, task_id=f"t{k}-{i}")

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()

    entries = led.entries()
    usd = [e for e in entries if e.usd]
    tok = [e for e in entries if e.token]
    assert len(usd) == N_THREADS * PER          # no USD entry lost
    assert len(tok) == N_THREADS * PER          # no token entry lost
    assert led.total_usd_cents() == N_THREADS * PER   # exact total
    # sequence numbers are unique (no collision under concurrency)
    seqs = [e.seq for e in entries]
    assert len(set(seqs)) == len(seqs)
