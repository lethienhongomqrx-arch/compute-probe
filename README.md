# compute-probe

Platform-neutral CPU capability probe.

`bench.py` reads the container's cgroup CPU quota, measures the parallel
speedup curve with independent processes, and reports single-thread
throughput plus memcpy bandwidth. It emits one JSON line so results from
different platforms can be compared directly.

Run locally:

    python3 bench.py --label local

The CircleCI pipeline runs it across several resource classes to compare
advertised vCPU counts against delivered throughput.
