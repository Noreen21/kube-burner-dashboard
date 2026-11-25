#!/usr/bin/env python3
"""
Process a kube-burner collected-metrics-<uuid> directory.

kube-burner writes one JSON file per metric, every file a flat ARRAY of
documents (the same documents it would have POSTed to OpenSearch). There are
four shapes:

  scalar      1 document, no .labels      e.g. max-cpu-etcd.json
  timeseries  N documents, each .labels   e.g. podStatusCount.json
  measurement podLatency{,Quantiles}Measurement-<job>.json
  meta        jobSummary.json, alert.json, nodeRoles.json, nodeStatus.json

Every document carries .uuid, .query, .metricName, .jobName and .metadata,
so a file is self-describing: .query tells you exactly what PromQL produced
it, which matters because the aggregation window is baked into the query
(e.g. "[2631s:]" = the whole run).

Usage:
  process_metrics.py <collected-metrics-dir>              # headline report
  process_metrics.py <dir> --all-scalars                  # every scalar
  process_metrics.py <dir> --csv out/                     # timeseries -> CSV
  process_metrics.py <dir> --show podStatusCount          # one series
"""

import argparse
import csv
import json
import sys
from pathlib import Path

SPECIAL = {"jobSummary.json", "alert.json", "nodeRoles.json",
           "nodeStatus.json", "etcdVersion.json"}

HEADLINE = [
    ("etcd", ["max-cpu-etcd", "cpu-etcd", "max-memory-etcd", "memory-etcd",
              "max-99thEtcdDiskWalFsync", "max-99thEtcdDiskBackendCommit",
              "max-99thEtcdRoundTripTime", "max-99thEtcdCompaction",
              "max-99thEtcdDefrag"]),
    ("kube-apiserver", ["max-cpu-kube-apiserver", "cpu-kube-apiserver",
                        "max-memory-kube-apiserver", "memory-kube-apiserver",
                        "max-ro-apicalls-latency", "avg-ro-apicalls-latency",
                        "max-mutating-apicalls-latency",
                        "avg-mutating-apicalls-latency"]),
    ("OVN / networking", ["max-cpu-ovn-control-plane", "max-memory-ovn-control-plane",
                          "max-cpu-ovnkube-node", "max-memory-ovnkube-node",
                          "max-cpu-multus"]),
    ("Nodes", ["max-cpu-masters", "max-cpu-workers", "max-memory-masters",
               "max-memory-workers", "max-cpu-cluster-usage-ratio",
               "max-memory-cluster-usage-ratio"]),
    ("Kubelet / CRI-O", ["max-cpu-kubelet", "max-memory-kubelet",
                         "max-cpu-crio", "max-memory-crio"]),
    ("Controllers", ["max-cpu-kube-controller-manager",
                     "max-memory-kube-controller-manager",
                     "max-cpu-openshift-apiserver",
                     "max-cpu-openshift-controller-manager"]),
]


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def fmt(name, v, query=""):
    """
    Render a raw value with its real unit.

    The metric NAME alone is not enough and gets two cases wrong:
      * "ratio" metrics are also named "...memory..."
        (max-memory-cluster-usage-ratio is a ratio, not bytes)
      * some etcd timings are already milliseconds
        (etcd_debugging_mvcc_db_compaction_total_duration_milliseconds_sum)
        while others are seconds (etcd_disk_backend_defrag_duration_seconds_sum)
    So ratio is tested first, and the time unit is taken from the PromQL in
    .query, which is authoritative.
    """
    if not isinstance(v, (int, float)):
        return str(v)
    n, q = name.lower(), (query or "").lower()

    if "ratio" in n:
        return f"{v * 100:.1f}%"
    if "memory" in n or "rss" in n:
        return f"{v / 2**30:.2f} GiB" if v >= 2**30 else f"{v / 2**20:.0f} MiB"
    if "cpu" in n:
        return f"{v:.3f} cores"
    if "latency" in n or "etcd" in n or "duration" in n:
        if "milliseconds" in q:
            return f"{v:.2f} ms"          # already ms; do not scale
        if "seconds" in q or not q:
            return f"{v * 1000:.2f} ms"
        return f"{v:,.4g}"
    if "rate" in n:
        return f"{v:.2f}/s"
    return f"{v:,.4g}"


def classify(docs):
    if not isinstance(docs, list) or not docs:
        return "empty"
    if len(docs) == 1 and "labels" not in docs[0]:
        return "scalar"
    return "timeseries"


def scan(mdir):
    scalars, series = {}, {}
    for f in sorted(Path(mdir).glob("*.json")):
        if f.name in SPECIAL or "Measurement-" in f.name:
            continue
        docs = load(f)
        kind = classify(docs)
        if kind == "scalar":
            scalars[f.stem] = docs[0]
        elif kind == "timeseries":
            series[f.stem] = docs
    return scalars, series


def report(mdir, args):
    mdir = Path(mdir)
    out = []

    # ---- run summary
    js = load(mdir / "jobSummary.json")
    if js:
        s = js[0]
        cfg = s.get("jobConfig", {})
        churn = cfg.get("churnConfig", {})
        out.append("=" * 70)
        out.append("RUN SUMMARY  (jobSummary.json)")
        out.append("=" * 70)
        out.append(f"  uuid          {s.get('uuid')}")
        out.append(f"  kube-burner   {s.get('version')}")
        out.append(f"  cluster       {s.get('clusterName')} "
                   f"({s.get('clusterType')}, {s.get('controlPlaneArch')})")
        out.append(f"  passed        {s.get('passed')}")
        out.append(f"  elapsed       {s.get('elapsedTime')}s")
        out.append(f"  iterations    {cfg.get('jobIterations')}  "
                   f"qps={cfg.get('qps')} burst={cfg.get('burst')} gc={cfg.get('gc')}")
        out.append(f"  achievedQps   {s.get('achievedQps')}")
        # churnConfig is present with percent/delay/mode defaults even when
        # churn never ran -- duration is the field that says it happened.
        # Gating on the dict alone printed "churn 0s" and a
        # "None -> None" window for every non-churn workload.
        if churn.get("duration"):
            out.append(f"  churn         {churn['duration'] / 1e9:.0f}s "
                       f"@{churn.get('percent')}% every "
                       f"{churn.get('delay', 0) / 1e9:.0f}s "
                       f"mode={churn.get('mode')}")
            out.append(f"  churn window  {s.get('churnStartTimestamp')} -> "
                       f"{s.get('churnEndTimestamp')}")
        else:
            out.append("  churn         not enabled")
        out.append("")

    # ---- alerts
    alerts = load(mdir / "alert.json")
    out.append("=" * 70)
    out.append(f"ALERTS  (alert.json) — {len(alerts) if alerts else 0} fired")
    out.append("=" * 70)
    if alerts:
        for a in sorted(alerts, key=lambda x: x.get("timestamp", "")):
            out.append(f"  {a.get('timestamp')}  [{a.get('severity')}]  "
                       f"{a.get('description')}")
    else:
        out.append("  none")
    out.append("")

    # ---- pod latency quantiles
    q = next(iter(mdir.glob("podLatencyQuantilesMeasurement-*.json")), None)
    if q:
        docs = load(q) or []
        out.append("=" * 70)
        out.append(f"POD LATENCY QUANTILES  ({q.name})   milliseconds")
        out.append("=" * 70)
        out.append(f"  {'condition':<28}{'P50':>8}{'P95':>8}{'P99':>8}"
                   f"{'max':>8}{'avg':>8}")
        for d in docs:
            out.append(f"  {d.get('quantileName', '?'):<28}"
                       f"{d.get('P50', 0):>8}{d.get('P95', 0):>8}"
                       f"{d.get('P99', 0):>8}{d.get('max', 0):>8}"
                       f"{d.get('avg', 0):>8}")
        out.append("")

    # ---- scalars
    scalars, series = scan(mdir)
    out.append("=" * 70)
    out.append(f"HEADLINE METRICS   ({len(scalars)} scalar, "
               f"{len(series)} time-series files)")
    out.append("=" * 70)
    shown = set()
    for group, names in HEADLINE:
        rows = [(n, scalars[n]["value"], scalars[n].get("query", ""))
                for n in names if n in scalars]
        if not rows:
            continue
        out.append(f"\n  -- {group} --")
        for n, v, q in rows:
            out.append(f"  {n:<40}{fmt(n, v, q):>18}")
            shown.add(n)

    if args.all_scalars:
        rest = sorted(set(scalars) - shown)
        if rest:
            out.append(f"\n  -- other ({len(rest)}) --")
            for n in rest:
                out.append(f"  {n:<40}"
                           f"{fmt(n, scalars[n]['value'], scalars[n].get('query','')):>18}")
    else:
        rest = len(set(scalars) - shown)
        if rest:
            out.append(f"\n  ({rest} more scalars; --all-scalars to show)")
    out.append("")

    # ---- largest series, for orientation
    out.append("=" * 70)
    out.append("LARGEST TIME SERIES  (--csv to export, --show NAME to inspect)")
    out.append("=" * 70)
    for n, docs in sorted(series.items(), key=lambda kv: -len(kv[1]))[:12]:
        lbls = sorted(docs[0].get("labels", {}).keys())
        out.append(f"  {len(docs):>7}  {n:<38} labels: {','.join(lbls) or '-'}")
    out.append("")

    return "\n".join(out), scalars, series


def show_series(series, name, limit):
    docs = series.get(name)
    if docs is None:
        print(f"no such series: {name}", file=sys.stderr)
        print(f"available: {', '.join(sorted(series))}", file=sys.stderr)
        return 1
    print(f"# {name}  ({len(docs)} samples)")
    print(f"# query: {docs[0].get('query', '')}")
    for d in docs[:limit]:
        lbl = ",".join(f"{k}={v}" for k, v in sorted(
            d.get("labels", {}).items()))
        print(f"{d.get('timestamp')}  {lbl:<50} {d.get('value')}")
    if len(docs) > limit:
        print(f"... {len(docs) - limit} more (raise --limit)")
    return 0


def export_csv(series, outdir):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for name, docs in series.items():
        keys = sorted({k for d in docs for k in d.get("labels", {})})
        with (outdir / f"{name}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", *keys, "value"])
            for d in docs:
                lb = d.get("labels", {})
                w.writerow([d.get("timestamp"),
                            *[lb.get(k, "") for k in keys],
                            d.get("value")])
        n += 1
    print(f"wrote {n} CSV files to {outdir}")


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("metricsdir")
    p.add_argument("--all-scalars", action="store_true")
    p.add_argument("--csv", metavar="OUTDIR")
    p.add_argument("--show", metavar="NAME")
    p.add_argument("--limit", type=int, default=40)
    args = p.parse_args()

    if not Path(args.metricsdir).is_dir():
        print(f"not a directory: {args.metricsdir}", file=sys.stderr)
        return 2

    text, scalars, series = report(args.metricsdir, args)

    if args.show:
        return show_series(series, args.show, args.limit)

    print(text)
    if args.csv:
        export_csv(series, args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
