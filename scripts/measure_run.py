"""Run a command and report wall time, process-tree CPU time and peak RSS."""
import sys
import time

import psutil

proc = psutil.Popen(sys.argv[1:])
start = time.perf_counter()
peak_rss = 0
cpu = 0.0
seen: dict[int, float] = {}
while proc.poll() is None:
    try:
        tree = [proc, *proc.children(recursive=True)]
        rss = 0
        for p in tree:
            try:
                t = p.cpu_times()
                seen[p.pid] = t.user + t.system
                rss += p.memory_info().rss
            except psutil.Error:
                pass
        peak_rss = max(peak_rss, rss)
    except psutil.Error:
        pass
    time.sleep(0.2)
wall = time.perf_counter() - start
cpu = sum(seen.values())
print(
    f"wall_s={wall:.1f} cpu_s={cpu:.1f} avg_cores={cpu / wall:.2f} "
    f"peak_rss_gb={peak_rss / 2**30:.2f} exit={proc.returncode}"
)
sys.exit(proc.returncode or 0)
