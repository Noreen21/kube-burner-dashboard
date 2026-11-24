#!/usr/bin/env python3
"""
Build a self-contained HTML dashboard from a kube-burner
collected-metrics-<uuid> directory.

Output is ONE .html file with the data inlined and charts drawn as SVG by
vanilla JS. No CDN, no server, no network: scp it anywhere and open it.

Usage:
  build_dashboard.py <collected-metrics-dir> [-o dashboard.html]
                     [--points N]   # per-series downsample cap (default 280)
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- data load


def load(p):
    try:
        with open(p) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else [d]
    except (OSError, json.JSONDecodeError):
        return []


def ts(doc):
    t = doc.get("timestamp", "")
    for f in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(t, f).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def grouped(docs, key=None, how="sum"):
    """
    Collapse documents into {series_name: {epoch: value}}.

    `key` is a label to split series on; None means one combined series.
    `how` folds several label-sets sharing a timestamp: sum (counts, rates),
    max (latency - the worst pod is the signal), mean (utilisation).
    """
    out = {}
    for d in docs:
        t, v = ts(d), d.get("value")
        if t is None or not isinstance(v, (int, float)):
            continue
        name = str(d.get("labels", {}).get(key, "")) if key else "value"
        if key and not name:
            continue
        bucket = out.setdefault(name, {})
        if t in bucket:
            a, n = bucket[t]
            bucket[t] = (max(a, v) if how == "max" else a + v, n + 1)
        else:
            bucket[t] = (v, 1)
    res = {}
    for name, pts in out.items():
        res[name] = {t: (s / n if how == "mean" else s)
                     for t, (s, n) in pts.items()}
    return res


def downsample(pairs, cap, how="mean"):
    """Bucket-average (or -max) a sorted [(t,v)] list down to <= cap points."""
    if len(pairs) <= cap:
        return pairs
    t0, t1 = pairs[0][0], pairs[-1][0]
    span = (t1 - t0) or 1
    buckets = {}
    for t, v in pairs:
        b = min(cap - 1, int((t - t0) / span * cap))
        buckets.setdefault(b, []).append((t, v))
    out = []
    for b in sorted(buckets):
        vs = buckets[b]
        tt = sum(x[0] for x in vs) / len(vs)
        vv = (max(x[1] for x in vs) if how == "max"
              else sum(x[1] for x in vs) / len(vs))
        out.append((tt, vv))
    return out


def series(mdir, fname, key=None, how="sum", scale=1.0, top=None,
           cap=280, rename=None):
    docs = load(Path(mdir) / f"{fname}.json")
    if not docs:
        return []
    g = grouped(docs, key, how)
    out = []
    for name, pts in g.items():
        pairs = sorted(pts.items())
        pairs = [(t, v * scale) for t, v in pairs]
        out.append({"name": rename(name) if rename else name,
                    "peak": max((v for _, v in pairs), default=0),
                    "points": downsample(pairs, cap,
                                         "max" if how == "max" else "mean")})
    # A series that never leaves zero is clutter: it spends a categorical
    # slot and a legend entry to draw a flat line on the baseline.
    out = [s for s in out if s["peak"] != 0]
    out.sort(key=lambda s: -s["peak"])
    if top:
        out = out[:top]
    return out


def final_counts(mdir):
    """
    Object totals at end of run, as BARS not lines.

    secretCount/configmapCount/serviceCount/deploymentCount/routeCount are
    instant `count(...)` queries scraped exactly once (n=1), so there is no
    series to plot -- a one-point path is a bare moveto and draws nothing.
    namespaceCount is a real series, so its last sample is used here and the
    series itself gets its own panel.
    """
    out = []
    for fname, label in (("secretCount", "Secrets"),
                         ("configmapCount", "ConfigMaps"),
                         ("serviceCount", "Services"),
                         ("deploymentCount", "Deployments"),
                         ("namespaceCount", "Namespaces"),
                         ("routeCount", "Routes")):
        docs = load(Path(mdir) / f"{fname}.json")
        vals = [(ts(d) or 0, d.get("value")) for d in docs
                if isinstance(d.get("value"), (int, float))]
        if not vals:
            continue
        # namespaceCount is split by phase; keep Active only.
        if fname == "namespaceCount":
            act = [(ts(d) or 0, d["value"]) for d in docs
                   if d.get("labels", {}).get("phase") == "Active"
                   and isinstance(d.get("value"), (int, float))]
            vals = act or vals
        out.append({"name": label, "value": max(vals)[1]})
    out.sort(key=lambda b: -b["value"])
    return out


def cpu_util(mdir, fname, cap):
    """node_cpu_seconds_total by mode -> utilisation % = (total-idle)/total."""
    docs = load(Path(mdir) / f"{fname}.json")
    per_t = {}
    for d in docs:
        t, v = ts(d), d.get("value")
        if t is None or not isinstance(v, (int, float)):
            continue
        mode = d.get("labels", {}).get("mode", "")
        tot, idle = per_t.get(t, (0.0, 0.0))
        per_t[t] = (tot + v, idle + (v if mode == "idle" else 0.0))
    pairs = sorted((t, (1 - i / tt) * 100 if tt else 0)
                   for t, (tt, i) in per_t.items())
    return downsample(pairs, cap)


# ---------------------------------------------------------------- panels

def build_panels(mdir, cap):
    P = []

    P.append({
        "id": "objects", "title": "Object count at end of run", "kind": "bar",
        "sub": "Cluster-wide totals from kube-state-metrics. Scraped once, "
               "so these are final values, not a series.",
        "unit": "", "bars": final_counts(mdir)})

    ns = series(mdir, "namespaceCount", "phase", "sum", cap=cap)
    ns = [s for s in ns if s["name"] == "Active"] or ns[:1]
    if ns:
        ns[0]["name"] = "Active namespaces"
    P.append({
        "id": "ns", "title": "Active namespaces",
        "sub": "The sawtooth in the shaded band is churn deleting and "
               "recreating 3 namespaces per round.",
        "unit": "", "panels": ns})

    P.append({
        "id": "pods", "title": "Pods by phase",
        "sub": "sum(kube_pod_status_phase) by (phase), cluster-wide.",
        "unit": "", "panels": series(mdir, "podStatusCount", "phase",
                                     "sum", cap=cap)})

    P.append({
        "id": "apirate", "title": "API request rate by verb",
        "sub": "irate(apiserver_request_total) excluding WATCH, summed per verb.",
        "unit": "/s", "panels": series(mdir, "APIRequestRate", "verb",
                                       "sum", top=6, cap=cap)})

    ro = series(mdir, "readOnlyAPICallsLatency", None, "max",
                scale=1000, cap=cap)
    mu = series(mdir, "mutatingAPICallsLatency", None, "max",
                scale=1000, cap=cap)
    lat = []
    if ro:
        ro[0]["name"] = "Read-only (LIST/GET)"
        lat.append(ro[0])
    if mu:
        mu[0]["name"] = "Mutating (POST/PUT/PATCH/DELETE)"
        lat.append(mu[0])
    P.append({
        "id": "apilat", "title": "API call latency P99",
        "sub": "Worst resource at each sample. Same unit, so one shared axis.",
        "unit": "ms", "panels": lat})

    etcd = []
    for f, label in (("99thEtcdDiskWalFsyncDurationSeconds", "WAL fsync"),
                     ("99thEtcdDiskBackendCommitDurationSeconds",
                      "Backend commit"),
                     ("99thEtcdRoundTripTimeSeconds", "Peer round-trip")):
        s = series(mdir, f, None, "max", scale=1000, cap=cap)
        if s:
            s[0]["name"] = label
            etcd.append(s[0])
    P.append({
        "id": "etcd", "title": "etcd P99 disk & peer latency",
        "sub": "Worst member at each sample. Alert thresholds: fsync 10 ms, "
               "commit 25 ms.", "unit": "ms", "panels": etcd})

    cpu = []
    for f, label in (("nodeCPU-Masters", "Control plane"),
                     ("nodeCPU-AggregatedWorkers", "Workers")):
        pts = cpu_util(mdir, f, cap)
        if pts:
            cpu.append({"name": label, "peak": max(v for _, v in pts),
                        "points": pts})
    P.append({
        "id": "nodecpu", "title": "Node CPU utilisation",
        "sub": "(total - idle) / total across node_cpu_seconds_total modes.",
        "unit": "%", "panels": cpu})

    mem = []
    for f, label in (("nodeMemoryUtilization-Masters", "Control plane"),
                     ("nodeMemoryUtilization-AggregatedWorkers", "Workers")):
        s = series(mdir, f, None, "mean", scale=1 / 2**30, cap=cap)
        if s:
            s[0]["name"] = label
            mem.append(s[0])
    P.append({
        "id": "nodemem", "title": "Node memory in use",
        "sub": "MemTotal - MemAvailable, averaged per node in the role.",
        "unit": " GiB", "panels": mem})

    return [p for p in P if p.get("panels") or p.get("bars")]


def build_meta(mdir):
    js = load(Path(mdir) / "jobSummary.json")
    s = js[0] if js else {}
    cfg = s.get("jobConfig", {})
    churn = cfg.get("churnConfig") or {}
    alerts = sorted(load(Path(mdir) / "alert.json"),
                    key=lambda a: a.get("timestamp", ""))
    q = next(iter(Path(mdir).glob("podLatencyQuantilesMeasurement-*.json")), None)
    lat = load(q) if q else []
    return {
        "uuid": s.get("uuid", ""),
        "version": s.get("version", ""),
        "cluster": s.get("clusterName", ""),
        "elapsed": s.get("elapsedTime", 0),
        "qps": s.get("achievedQps", 0),
        "iterations": cfg.get("jobIterations", 0),
        "passed": s.get("passed"),
        "churnStart": s.get("churnStartTimestamp", ""),
        "churnEnd": s.get("churnEndTimestamp", ""),
        # Everything the tiles annotate with must come from the run itself,
        # never be baked into the template.
        "reqQps": cfg.get("qps"),
        "churnPercent": churn.get("percent"),
        "churnDurationS": (churn.get("duration") or 0) / 1e9 or None,
        "churnDelayS": (churn.get("delay") or 0) / 1e9 or None,
        "churnMode": churn.get("mode"),
        "gc": cfg.get("gc"),
        "alerts": [{"t": a.get("timestamp"), "sev": a.get("severity"),
                    "desc": a.get("description")} for a in alerts],
        "latency": [{"name": d.get("quantileName"), "P50": d.get("P50"),
                     "P95": d.get("P95"), "P99": d.get("P99"),
                     "max": d.get("max"), "avg": d.get("avg")} for d in lat],
    }


# ---------------------------------------------------------------- html

HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>kube-burner · __UUID8__</title>
<style>
/* Palette: dataviz reference instance. Categorical slots used in fixed
   order, never cycled. Validated with validate_palette.py (Python twin of
   the skill's validator - no Node on either host): adjacent CVD dE 9.1
   light / 8.4 dark, normal-vision 19.6 / 19.3, both PASS. Three light slots
   (aqua, yellow, magenta) sit below 3:1 on the light surface, so the
   relief rule applies - every panel ships direct labels and a table view. */
.viz-root{
  color-scheme:light;
  --surface-1:#fcfcfb; --page:#f9f9f7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --band:rgba(11,11,11,.045);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --s4:#eda100; --s5:#e87ba4; --s6:#008300;
  --warning:#fab219; --critical:#d03b3b; --good:#0ca30c;
}
@media (prefers-color-scheme:dark){ :root:where(:not([data-theme="light"])) .viz-root{
  color-scheme:dark;
  --surface-1:#1a1a19; --page:#0d0d0d;
  --text-primary:#f0efec; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --band:rgba(255,255,255,.055);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --s4:#c98500; --s5:#d55181; --s6:#0ca30c;
}}
:root[data-theme="dark"] .viz-root{
  color-scheme:dark;
  --surface-1:#1a1a19; --page:#0d0d0d;
  --text-primary:#f0efec; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --band:rgba(255,255,255,.055);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --s4:#c98500; --s5:#d55181; --s6:#0ca30c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.viz-root{background:var(--page);color:var(--text-primary);padding:28px 24px 64px;min-height:100vh}
.wrap{max-width:1360px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:21px;font-weight:650;margin:0 0 4px}
.sub{color:var(--text-secondary);font-size:13px}
.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
button{font:inherit;color:var(--text-secondary);background:var(--surface-1);
  border:1px solid var(--border);border-radius:7px;padding:5px 11px;cursor:pointer}
button:hover{color:var(--text-primary)}
button[aria-pressed="true"]{color:var(--text-primary);border-color:var(--axis)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:22px 0 26px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:11px;padding:14px 16px}
.tile .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.055em}
.tile .v{font-size:27px;font-weight:620;margin-top:5px;line-height:1.15}
.tile .n{color:var(--text-secondary);font-size:12px;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(560px,1fr));gap:16px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:11px;padding:16px 18px 10px}
.card h2{font-size:14px;font-weight:620;margin:0 0 2px}
.card .cs{color:var(--text-secondary);font-size:12px;margin:0 0 10px}
.chead{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0 2px}
.lg{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary)}
.sw{width:11px;height:11px;border-radius:3px;flex:none}
svg{display:block;width:100%;overflow:visible}
.tbl{display:none;max-height:300px;overflow:auto;margin-top:8px}
.tbl table{border-collapse:collapse;width:100%;font-size:12px}
.tbl th,.tbl td{text-align:right;padding:4px 8px;border-bottom:1px solid var(--grid);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl th:first-child,.tbl td:first-child{text-align:left}
.tbl th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--surface-1)}
.card.showtable .tbl{display:block}
.card.showtable .plot{display:none}
.tip{position:fixed;pointer-events:none;z-index:40;background:var(--surface-1);
  border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;
  box-shadow:0 6px 22px rgba(0,0,0,.17);opacity:0;transition:opacity .09s;min-width:150px}
.tip .tt{color:var(--muted);font-size:11px;margin-bottom:5px}
.tip .tr{display:flex;align-items:center;gap:7px;justify-content:space-between}
.tip .tl{display:flex;align-items:center;gap:6px;color:var(--text-secondary)}
.tip .tv{font-variant-numeric:tabular-nums;color:var(--text-primary)}
.alerts{margin-top:16px}
.alerts table{border-collapse:collapse;width:100%;font-size:13px;background:var(--surface-1)}
.alerts th,.alerts td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--grid)}
.alerts th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.sev{display:inline-flex;align-items:center;gap:6px;font-weight:560}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
footer{color:var(--muted);font-size:12px;margin-top:30px;line-height:1.7}
</style></head>
<body><div class="viz-root"><div class="wrap">

<header>
  <div>
    <h1 id="mtitle"></h1>
    <div class="sub">kube-burner <span id="mver"></span> ·
      cluster <span id="mclu"></span> ·
      run <span class="mono" id="muid"></span></div>
  </div>
  <button id="theme" aria-pressed="false">Dark mode</button>
</header>

<div class="tiles" id="tiles"></div>
<div class="grid" id="grid"></div>

<div class="alerts">
  <h2 style="font-size:14px;font-weight:620;margin:0 0 8px">Alerts fired during the run</h2>
  <table><thead><tr><th>Time (UTC)</th><th>Severity</th><th>Description</th></tr></thead>
  <tbody id="alertbody"></tbody></table>
</div>

<footer>
  Shaded band marks the churn phase; the unshaded region before it is the create ramp.
  Every panel has a <strong>Table</strong> toggle — no value is reachable only by hover.
  Series colours follow the dataviz reference palette in fixed slot order.
</footer>
</div></div>

<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const SLOT = ["--s1","--s2","--s3","--s4","--s5","--s6"];
const cssv = n => getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(n).trim();
const pad  = n => String(n).padStart(2,"0");
const hhmm = t => { const d=new Date(t*1000); return pad(d.getUTCHours())+":"+pad(d.getUTCMinutes()); };
const hms  = t => { const d=new Date(t*1000); return hhmm(t)+":"+pad(d.getUTCSeconds()); };

function fmt(v,u){
  if(v==null||isNaN(v)) return "—";
  const a=Math.abs(v);
  let s = a>=1000 ? v.toFixed(0) : a>=100 ? v.toFixed(1) : a>=1 ? v.toFixed(2) : v.toFixed(3);
  if(a>=1000) s=Number(s).toLocaleString("en-US");
  return s+(u||"");
}
function niceTicks(lo,hi,n){
  if(hi<=lo) hi=lo+1;
  const raw=(hi-lo)/n, mag=Math.pow(10,Math.floor(Math.log10(raw)));
  const norm=raw/mag, step=(norm<=1?1:norm<=2?2:norm<=5?5:10)*mag;
  const out=[]; for(let v=Math.ceil(lo/step)*step; v<=hi+1e-9; v+=step) out.push(v);
  return out;
}

const W=660,H=240,ML=58,MR=96,MT=12,MB=28;

function drawBar(card,p){
  const plot=card.querySelector(".plot"), bars=p.bars;
  const N="http://www.w3.org/2000/svg";
  const mk=(t,a)=>{const e=document.createElementNS(N,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
  const BH=22, BG=10, LW=104, RW=64, h=bars.length*(BH+BG)+8;
  const svg=document.createElementNS(N,"svg");
  svg.setAttribute("viewBox",`0 0 ${W} ${h}`); svg.setAttribute("role","img");
  const max=Math.max(...bars.map(b=>b.value))||1;
  const c=cssv("--s1");                       // one series -> one colour
  const tip=document.getElementById("tip");
  bars.forEach((b,i)=>{
    const y=i*(BH+BG)+4, w=Math.max(2,(b.value/max)*(W-LW-RW));
    const lab=mk("text",{x:LW-10,y:y+BH/2+4,"text-anchor":"end","font-size":12,
      fill:cssv("--text-secondary")});
    lab.textContent=b.name; svg.appendChild(lab);
    const r=mk("rect",{x:LW,y,width:w,height:BH,rx:4,fill:c});
    svg.appendChild(r);
    const v=mk("text",{x:LW+w+9,y:y+BH/2+4,"font-size":12,
      fill:cssv("--text-primary")});
    v.textContent=b.value.toLocaleString("en-US"); svg.appendChild(v);
    const hit=mk("rect",{x:0,y:y-BG/2,width:W,height:BH+BG,fill:"transparent"});
    hit.addEventListener("pointermove",ev=>{
      tip.innerHTML=`<div class="tt">${b.name}</div><div class="tr">`+
        `<span class="tl"><span class="sw" style="background:${c}"></span>objects</span>`+
        `<span class="tv">${b.value.toLocaleString("en-US")}</span></div>`;
      tip.style.opacity=1;
      const tw=tip.offsetWidth,th=tip.offsetHeight;
      let L=ev.clientX+14; if(L+tw>innerWidth-8) L=ev.clientX-tw-14;
      tip.style.left=Math.max(8,L)+"px";
      tip.style.top=Math.min(Math.max(8,ev.clientY-th/2),innerHeight-th-8)+"px";
    });
    hit.addEventListener("pointerleave",()=>tip.style.opacity=0);
    svg.appendChild(hit);
  });
  plot.innerHTML=""; plot.appendChild(svg);
  card.querySelector(".legend").innerHTML="";   // single series: title names it
  card.querySelector(".tbl").innerHTML=
    "<table><thead><tr><th>Object</th><th>Count</th></tr></thead><tbody>"+
    bars.map(b=>`<tr><td>${b.name}</td><td>${b.value.toLocaleString("en-US")}</td></tr>`).join("")+
    "</tbody></table>";
}

function drawPanel(card,p){
  if(p.kind==="bar") return drawBar(card,p);
  const plot=card.querySelector(".plot");
  const all=p.panels.flatMap(s=>s.points);
  if(!all.length) return;
  const t0=Math.min(...all.map(d=>d[0])), t1=Math.max(...all.map(d=>d[0]));
  let vmax=Math.max(...all.map(d=>d[1])), vmin=Math.min(0,...all.map(d=>d[1]));
  if(vmax===vmin) vmax=vmin+1;
  const ticks=niceTicks(vmin,vmax,4);
  const hi=Math.max(vmax,ticks[ticks.length-1]);
  const X=t=>ML+(t-t0)/((t1-t0)||1)*(W-ML-MR);
  const Y=v=>MT+(1-(v-vmin)/((hi-vmin)||1))*(H-MT-MB);

  const N="http://www.w3.org/2000/svg";
  const svg=document.createElementNS(N,"svg");
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
  svg.setAttribute("role","img");
  const mk=(t,a)=>{const e=document.createElementNS(N,t);for(const k in a)e.setAttribute(k,a[k]);return e;};

  // churn band — a phase annotation, not a series
  if(DATA.meta.churnStartEpoch){
    const cs=Math.max(t0,DATA.meta.churnStartEpoch), ce=Math.min(t1,DATA.meta.churnEndEpoch);
    if(ce>cs) svg.appendChild(mk("rect",{x:X(cs),y:MT,width:X(ce)-X(cs),
      height:H-MT-MB,fill:cssv("--band")}));
  }
  // gridlines + y labels (solid hairlines, recessive)
  ticks.forEach(v=>{
    svg.appendChild(mk("line",{x1:ML,x2:W-MR,y1:Y(v),y2:Y(v),stroke:cssv("--grid"),"stroke-width":1}));
    const l=mk("text",{x:ML-9,y:Y(v)+4,"text-anchor":"end","font-size":11,fill:cssv("--muted")});
    l.textContent=fmt(v,p.unit); svg.appendChild(l);
  });
  svg.appendChild(mk("line",{x1:ML,x2:W-MR,y1:H-MB,y2:H-MB,stroke:cssv("--axis"),"stroke-width":1}));
  const steps=5;
  for(let i=0;i<=steps;i++){
    const t=t0+(t1-t0)*i/steps;
    const l=mk("text",{x:X(t),y:H-MB+16,"text-anchor":"middle","font-size":11,fill:cssv("--muted")});
    l.textContent=hhmm(t); svg.appendChild(l);
  }

  // series: 2px lines
  p.panels.forEach((s,i)=>{
    const c=cssv(SLOT[i%SLOT.length]);
    const d=s.points.map((pt,k)=>(k?"L":"M")+X(pt[0]).toFixed(1)+" "+Y(pt[1]).toFixed(1)).join(" ");
    svg.appendChild(mk("path",{d,fill:"none",stroke:c,"stroke-width":2,
      "stroke-linejoin":"round","stroke-linecap":"round"}));
  });

  // Direct end-labels when <=4 series (legend is present regardless).
  // Labels are decluttered: ties at the baseline would otherwise overprint
  // (two flat-zero phases rendered as "Paiheg" before this was added).
  if(p.panels.length<=4){
    const want=p.panels.map((s,i)=>{
      const last=s.points[s.points.length-1];
      return {y:Y(last[1]), x:X(last[0])+8, i,
              txt:s.name.length>18?s.name.slice(0,17)+"…":s.name};
    }).sort((a,b)=>a.y-b.y);
    const GAP=12;
    for(let k=1;k<want.length;k++)
      if(want[k].y-want[k-1].y<GAP) want[k].y=want[k-1].y+GAP;
    const over=want.length?want[want.length-1].y-(H-MB):0;
    if(over>0) want.forEach(w=>w.y-=over);
    want.forEach(w=>{
      const tx=mk("text",{x:w.x,y:Math.max(MT+8,w.y)+4,"font-size":11,
        fill:cssv("--text-secondary")});
      tx.textContent=w.txt; svg.appendChild(tx);
    });
  }

  // crosshair + hover
  const cross=mk("line",{x1:0,x2:0,y1:MT,y2:H-MB,stroke:cssv("--axis"),
    "stroke-width":1,opacity:0});
  svg.appendChild(cross);
  const dots=p.panels.map((s,i)=>{
    const c=mk("circle",{r:4.5,fill:cssv(SLOT[i%SLOT.length]),
      stroke:cssv("--surface-1"),"stroke-width":2,opacity:0});
    svg.appendChild(c); return c;
  });
  const tip=document.getElementById("tip");
  svg.addEventListener("pointermove",ev=>{
    const r=svg.getBoundingClientRect();
    const sx=(ev.clientX-r.left)/r.width*W;
    if(sx<ML||sx>W-MR){ hide(); return; }
    const t=t0+(sx-ML)/(W-ML-MR)*(t1-t0);
    cross.setAttribute("x1",sx); cross.setAttribute("x2",sx); cross.setAttribute("opacity",1);
    let rows="",shown=0;
    p.panels.forEach((s,i)=>{
      let best=null,bd=1e18;
      for(const pt of s.points){const d=Math.abs(pt[0]-t); if(d<bd){bd=d;best=pt;}}
      if(!best||bd>(t1-t0)/18){dots[i].setAttribute("opacity",0);return;}
      dots[i].setAttribute("cx",X(best[0])); dots[i].setAttribute("cy",Y(best[1]));
      dots[i].setAttribute("opacity",1); shown++;
      rows+=`<div class="tr"><span class="tl"><span class="sw" style="background:${cssv(SLOT[i%SLOT.length])}"></span>${s.name}</span><span class="tv">${fmt(best[1],p.unit)}</span></div>`;
    });
    if(!shown){hide();return;}
    tip.innerHTML=`<div class="tt">${hms(t)} UTC</div>`+rows;
    tip.style.opacity=1;
    const tw=tip.offsetWidth,th=tip.offsetHeight;
    let L=ev.clientX+14, T=ev.clientY-th/2;
    if(L+tw>innerWidth-8) L=ev.clientX-tw-14;
    tip.style.left=Math.max(8,L)+"px";
    tip.style.top=Math.min(Math.max(8,T),innerHeight-th-8)+"px";
  });
  function hide(){ tip.style.opacity=0; cross.setAttribute("opacity",0);
    dots.forEach(d=>d.setAttribute("opacity",0)); }
  svg.addEventListener("pointerleave",hide);
  plot.innerHTML=""; plot.appendChild(svg);

  // legend (always, >=2 series) + table twin
  const lg=card.querySelector(".legend");
  lg.innerHTML = p.panels.length<2 ? "" : p.panels.map((s,i)=>
    `<span class="lg"><span class="sw" style="background:${cssv(SLOT[i%SLOT.length])}"></span>${s.name}</span>`).join("");

  const times=[...new Set(all.map(d=>d[0]))].sort((a,b)=>a-b);
  const pick=times.filter((_,i)=>i%Math.max(1,Math.ceil(times.length/60))===0);
  card.querySelector(".tbl").innerHTML=
    "<table><thead><tr><th>Time (UTC)</th>"+p.panels.map(s=>`<th>${s.name}</th>`).join("")+
    "</tr></thead><tbody>"+pick.map(t=>{
      const cells=p.panels.map(s=>{
        let best=null,bd=1e18;
        for(const pt of s.points){const d=Math.abs(pt[0]-t);if(d<bd){bd=d;best=pt;}}
        return `<td>${best&&bd<=(t1-t0)/18?fmt(best[1],p.unit):"—"}</td>`;
      }).join("");
      return `<tr><td>${hms(t)}</td>${cells}</tr>`;
    }).join("")+"</tbody></table>";
}

function render(){
  const m=DATA.meta;
  mtitle.textContent=m.title||"kube-burner run";
  mver.textContent=(m.version||"").split("@")[0];
  mclu.textContent=m.cluster; muid.textContent=(m.uuid||"").slice(0,8);

  const ready=(m.latency||[]).find(l=>l.name==="Ready");
  const sevs=[...new Set((m.alerts||[]).map(a=>a.sev))];
  const mins=s=>s>=60?(s/60).toFixed(s%60?1:0)+" min":s+" s";
  const tiles=[
    ["Iterations",m.iterations||"—","job iterations"],
    ["Peak pods",m.peakPods||"—","cluster-wide"],
    ["Achieved QPS",(m.qps??0).toFixed(3),
      m.reqQps?`requested ${m.reqQps}`:"vs requested"],
    ["Pod ready P99",ready?(ready.P99/1000).toFixed(1)+" s":"—","P99 across all pods"],
    m.churnDurationS
      ? ["Churn",mins(m.churnDurationS),
         `${m.churnPercent}% every ${mins(m.churnDelayS)} · ${m.churnMode}`]
      : ["Elapsed",mins(m.elapsed||0),"total job time"],
    ["Alerts",(m.alerts||[]).length,sevs.length?sevs.join(", "):"none fired"],
  ];
  document.getElementById("tiles").innerHTML=tiles.map(([k,v,n])=>
    `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join("");

  document.getElementById("grid").innerHTML=DATA.panels.map(p=>
    `<div class="card" id="c-${p.id}">
       <div class="chead">
         <div><h2>${p.title}</h2><p class="cs">${p.sub}</p></div>
         <button class="tgl" aria-pressed="false">Table</button>
       </div>
       <div class="legend"></div><div class="plot"></div><div class="tbl"></div>
     </div>`).join("");

  DATA.panels.forEach(p=>{
    const card=document.getElementById("c-"+p.id);
    drawPanel(card,p);
    const b=card.querySelector(".tgl");
    b.addEventListener("click",()=>{
      const on=card.classList.toggle("showtable");
      b.setAttribute("aria-pressed",on); b.textContent=on?"Chart":"Table";
    });
  });

  const sev={warning:"--warning",critical:"--critical",info:"--good"};
  document.getElementById("alertbody").innerHTML=(m.alerts||[]).map(a=>
    `<tr><td class="mono">${(a.t||"").replace("T"," ").replace("Z","").split(".")[0]}</td>
     <td><span class="sev"><span class="dot" style="background:${cssv(sev[a.sev]||"--warning")}"></span>${a.sev}</span></td>
     <td>${a.desc}</td></tr>`).join("")||`<tr><td colspan="3">none</td></tr>`;
}

const tbtn=document.getElementById("theme");
tbtn.addEventListener("click",()=>{
  const dark=document.documentElement.getAttribute("data-theme")==="dark";
  document.documentElement.setAttribute("data-theme",dark?"light":"dark");
  tbtn.setAttribute("aria-pressed",!dark);
  tbtn.textContent=dark?"Dark mode":"Light mode";
  render();
});
matchMedia("(prefers-color-scheme:dark)").addEventListener("change",render);
render();
addEventListener("resize",()=>{DATA.panels.forEach(p=>drawPanel(document.getElementById("c-"+p.id),p));});
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("metricsdir")
    ap.add_argument("-o", "--out", default="dashboard.html")
    ap.add_argument("--points", type=int, default=280,
                    help="per-series downsample cap (default 280)")
    ap.add_argument("--title", default="kube-burner run",
                    help="dashboard heading (default: 'kube-burner run')")
    a = ap.parse_args()

    mdir = Path(a.metricsdir)
    if not mdir.is_dir():
        print(f"not a directory: {mdir}", file=sys.stderr)
        return 2

    panels = build_panels(mdir, a.points)
    meta = build_meta(mdir)

    def epoch(s):
        for f in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(s[:26] + "Z" if len(s) > 27 else s,
                                         f).replace(tzinfo=timezone.utc).timestamp()
            except (ValueError, TypeError):
                continue
        return None

    meta["churnStartEpoch"] = epoch(meta.get("churnStart", ""))
    meta["churnEndEpoch"] = epoch(meta.get("churnEnd", ""))

    pods = next((p for p in panels if p["id"] == "pods"), None)
    if pods:
        tot = {}
        for s in pods["panels"]:
            for t, v in s["points"]:
                tot[round(t)] = tot.get(round(t), 0) + v
        meta["peakPods"] = int(max(tot.values())) if tot else None

    meta["title"] = a.title

    html = (HTML.replace("__DATA__",
                         json.dumps({"meta": meta, "panels": panels},
                                    separators=(",", ":")))
                .replace("__UUID8__", (meta.get("uuid") or "")[:8]))
    Path(a.out).write_text(html)

    n = sum(len(p.get("panels", [])) for p in panels)
    pts = sum(len(s["points"]) for p in panels for s in p.get("panels", []))
    print(f"wrote {a.out}  ({len(panels)} panels, {n} series, {pts} points, "
          f"{len(html) / 1024:.0f} KiB)")
    for p in panels:
        if p.get("kind") == "bar":
            print(f"  {p['id']:<10} bar: "
                  f"{', '.join(b['name'] for b in p['bars'])}")
        else:
            print(f"  {p['id']:<10} {len(p['panels'])} series: "
                  f"{', '.join(s['name'] for s in p['panels'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
