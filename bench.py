#!/usr/bin/env python3
"""平台中立的 CPU 事實探測 + 吞吐量測。

只用 Python 標準庫，在任何 Linux 容器 / runner 內都能跑，輸出同一份 JSON
schema，所以 Modal 容器和 GitHub Actions runner 的結果可以直接 diff。

用法:
    python3 bench.py --label <名稱> [--quick]
"""
import argparse
import json
import multiprocessing as mp
import os
import platform
import subprocess
import sys
import time

# ---------------------------------------------------------------- 事實探測


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def facts():
    """不靠量測、直接從系統讀出來的硬事實。cgroup quota 是決定性證據。"""
    f = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "os_cpu_count": os.cpu_count(),
    }
    try:
        f["sched_affinity_count"] = len(os.sched_getaffinity(0))
    except Exception:
        f["sched_affinity_count"] = None

    ci = _read("/proc/cpuinfo") or ""
    models = [l.split(":", 1)[1].strip() for l in ci.splitlines() if l.startswith("model name")]
    f["cpu_model"] = models[0] if models else None
    f["cpuinfo_logical_processors"] = len(models)

    # physical id + core id 的組合數 = 可見的實體核心數
    sockets, cores = set(), set()
    cur_socket = None
    for line in ci.splitlines():
        if line.startswith("physical id"):
            cur_socket = line.split(":", 1)[1].strip()
            sockets.add(cur_socket)
        elif line.startswith("core id"):
            cores.add((cur_socket, line.split(":", 1)[1].strip()))
    f["sockets_visible"] = len(sockets) or None
    f["physical_cores_visible"] = len(cores) or None
    if f["physical_cores_visible"] and f["cpuinfo_logical_processors"]:
        f["threads_per_core"] = round(
            f["cpuinfo_logical_processors"] / f["physical_cores_visible"], 2
        )

    # ★ 決定性證據：cgroup CPU 配額。這直接告訴你被允許用幾個核心-秒/秒。
    v2 = _read("/sys/fs/cgroup/cpu.max")
    if v2:
        parts = v2.split()
        f["cgroup_version"] = 2
        f["cgroup_cpu_quota_cores"] = (
            "max" if parts[0] == "max" else round(int(parts[0]) / int(parts[1]), 4)
        )
        f["cgroup_cpu_max_raw"] = v2.strip()
    else:
        q = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        p = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q and p:
            qi, pi = int(q.strip()), int(p.strip())
            f["cgroup_version"] = 1
            f["cgroup_cpu_quota_cores"] = "max" if qi < 0 else round(qi / pi, 4)
            f["cgroup_cpu_max_raw"] = f"{qi} {pi}"
        else:
            f["cgroup_version"] = None
            f["cgroup_cpu_quota_cores"] = None

    # cgroup CPU shares / weight（相對權重，用來判斷是否被軟性壓低）
    for path in ("/sys/fs/cgroup/cpu.weight", "/sys/fs/cgroup/cpu/cpu.shares"):
        v = _read(path)
        if v:
            f["cgroup_cpu_weight_raw"] = v.strip()
            break

    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        m = _read(path)
        if m:
            m = m.strip()
            f["cgroup_mem_limit_gib"] = m if m == "max" else round(int(m) / 2**30, 3)
            break

    mi = _read("/proc/meminfo") or ""
    for line in mi.splitlines():
        if line.startswith("MemTotal"):
            f["meminfo_total_gib"] = round(int(line.split()[1]) / 2**20, 2)
            break

    for cmd in (["lscpu"], ["nproc"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            f[cmd[0]] = r.stdout.strip()
        except Exception as e:
            f[cmd[0]] = f"(unavailable: {e})"

    # 容器/環境識別
    f["env_hints"] = {
        k: os.environ.get(k)
        for k in (
            "MODAL_TASK_ID",
            "MODAL_ENVIRONMENT",
            "GITHUB_RUN_ID",
            "GITHUB_JOB",
            "RUNNER_NAME",
            "RUNNER_ARCH",
            "ImageOS",
        )
        if os.environ.get(k)
    }
    return f


# ---------------------------------------------------------------- CPU 吞吐

CHUNK = 20_000


def _burn(seconds, out):
    """純整數運算、不配置記憶體的忙迴圈。回報完成的迭代數。"""
    end = time.perf_counter() + seconds
    n = 0
    x = 123456789
    while time.perf_counter() < end:
        for _ in range(CHUNK):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        n += CHUNK
    out.put(n)


def _run_level(nproc, seconds):
    q = mp.Queue()
    procs = [mp.Process(target=_burn, args=(seconds, q)) for _ in range(nproc)]
    t0 = time.perf_counter()
    for p in procs:
        p.start()
    totals = [q.get() for _ in procs]
    for p in procs:
        p.join()
    wall = time.perf_counter() - t0
    return sum(totals) / wall, wall


def scaling(levels, seconds):
    """平行加速曲線。膝點 = 真正可用的核心數。"""
    base = None
    rows = []
    for n in levels:
        rate, wall = _run_level(n, seconds)
        if base is None:
            base = rate
        rows.append(
            {
                "processes": n,
                "ops_per_sec": round(rate),
                "speedup_vs_1": round(rate / base, 3),
                "wall_sec": round(wall, 2),
            }
        )
        print(f"  n={n:<4} ops/s={rate:>14,.0f}  speedup={rate/base:>6.2f}x", flush=True)
    return rows


def single_thread_score(seconds):
    """單執行緒絕對速度 —— 用來比兩邊的 CPU 世代，跟核心數無關。"""
    rate, _ = _run_level(1, seconds)
    return round(rate)


def memcpy_bandwidth(mib=512):
    """C 層級的大塊記憶體複製，量頻寬（避開 Python 逐次存取的開銷）。"""
    try:
        src = bytearray(mib * 1024 * 1024)
        t0 = time.perf_counter()
        dst = bytes(src)
        dt = time.perf_counter() - t0
        del dst, src
        return {"mib": mib, "sec": round(dt, 4), "gib_per_sec": round(mib / 1024 / dt, 2)}
    except MemoryError:
        return {"mib": mib, "error": "MemoryError"}


# ---------------------------------------------------------------- 主程式


def run_all(label, quick=False):
    seconds = 1.5 if quick else 3.0
    levels = [1, 2, 4, 8, 16] if quick else [1, 2, 3, 4, 6, 8, 12, 16, 20, 24, 32]

    print(f"=== {label} ===", flush=True)
    f = facts()
    print(
        f"  cpu_model={f.get('cpu_model')}\n"
        f"  logical={f.get('cpuinfo_logical_processors')} "
        f"physical_visible={f.get('physical_cores_visible')} "
        f"threads_per_core={f.get('threads_per_core')}\n"
        f"  os.cpu_count={f.get('os_cpu_count')} affinity={f.get('sched_affinity_count')}\n"
        f"  ★cgroup_cpu_quota_cores={f.get('cgroup_cpu_quota_cores')} "
        f"(raw={f.get('cgroup_cpu_max_raw')}, v{f.get('cgroup_version')})\n"
        f"  cgroup_mem_limit_gib={f.get('cgroup_mem_limit_gib')} "
        f"meminfo_total_gib={f.get('meminfo_total_gib')}",
        flush=True,
    )

    print("  -- scaling --", flush=True)
    rows = scaling(levels, seconds)
    best = max(rows, key=lambda r: r["ops_per_sec"])

    result = {
        "label": label,
        "ts": time.time(),
        "facts": f,
        "scaling": rows,
        "peak_speedup": best["speedup_vs_1"],
        "peak_at_processes": best["processes"],
        "single_thread_ops_per_sec": rows[0]["ops_per_sec"],
        "memcpy": memcpy_bandwidth(256 if quick else 512),
    }
    print(
        f"  → 峰值加速 {best['speedup_vs_1']}x @ n={best['processes']}"
        f"（= 實際可用核心當量）",
        flush=True,
    )
    print("BENCH_JSON:" + json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    try:  # Windows 終端是 cp950，不轉就會把中文與符號印成亂碼
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    mp.set_start_method("fork", force=True) if sys.platform != "win32" else None
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="local")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    r = run_all(a.label, a.quick)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=2)
