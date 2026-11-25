"""Seeress watcher — the activity logger (a spike).

Records real-Chrome tab events (CDP browser WebSocket via DevToolsActivePort)
plus the focus stream (AppleScript active-tab probe) to
~/.seeress/watch/spike-YYYYMMDD.jsonl. Everything stays on this machine.

  seeress watch-start                    install + load LaunchAgent
                                      (survives reboots and Chrome restarts)
  seeress watch-stop                     unload the logger
  seeress watch-analyze [--hours N] [--no-llm]
                                      mine the log: per-domain time, repeated
                                      visit sequences, flow proposals from the
                                      local brain (nemotron — nothing leaves)

The logger is deliberately dumb: append events, never interpret. All mining
happens in analyze(), offline, on your own file.
"""

import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

WATCH_DIR = Path.home() / ".seeress" / "watch"
PORT_FILE = Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"
POLL_FOCUS_S = 5.0
FOCUS_GAP_CAP_S = 180.0  # longer gaps = away, not browsing
LABEL = "ai.seeress.spike-watch"


# ---------------------------------------------------------------- logging ---


class Log:
    def __init__(self):
        WATCH_DIR.mkdir(parents=True, exist_ok=True)
        self.path = WATCH_DIR / f"spike-{time.strftime('%Y%m%d')}.jsonl"
        self._f = open(self.path, "a")
        self._last_tab_info: dict[str, tuple[str, str]] = {}  # target -> (url, title)

    def ev(self, kind: str, **kw):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **kw}
        self._f.write(json.dumps(rec) + "\n")
        self._f.flush()

    def tab_event(self, kind: str, target: str, url: str, title: str):
        """Dedup identical infoChanged repeats (title churn fires these)."""
        if kind == "nav":
            prev = self._last_tab_info.get(target)
            if prev == (url, title):
                return
            self._last_tab_info[target] = (url, title)
        elif kind == "tab_close":
            self._last_tab_info.pop(target, None)
        self.ev(kind, target=target, url=url, title=title)


def _read_port() -> tuple[int, str]:
    lines = PORT_FILE.read_text().splitlines()
    return int(lines[0]), lines[1].strip()


def _active_tab() -> tuple[str | None, str | None]:
    try:
        out = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "Google Chrome" to get {URL, title} '
                "of active tab of front window",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        url, _, title = out.partition(", ")
        return (url, title) if url else (None, None)
    except Exception:
        return None, None


async def _ws_loop(log: Log):
    import websockets  # in the venv via browser-use

    while True:
        try:
            port, path = _read_port()
            # No proxy: every tab's address and title pass this way, and a proxy that is
            # named would see them, even for a loopback address.
            async with websockets.connect(
                f"ws://127.0.0.1:{port}{path}", max_size=2**22, proxy=None
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Target.setDiscoverTargets",
                            "params": {"discover": True},
                        }
                    )
                )
                log.ev("connected", port=port)
                async for msg in ws:
                    m = json.loads(msg)
                    method = m.get("method", "")
                    if method == "Target.targetCreated":
                        ti = m["params"]["targetInfo"]
                        if ti.get("type") == "page":
                            log.tab_event(
                                "tab_open",
                                ti["targetId"][:8],
                                ti.get("url", ""),
                                ti.get("title", ""),
                            )
                    elif method == "Target.targetInfoChanged":
                        ti = m["params"]["targetInfo"]
                        if ti.get("type") == "page":
                            log.tab_event(
                                "nav", ti["targetId"][:8], ti.get("url", ""), ti.get("title", "")
                            )
                    elif method == "Target.targetDestroyed":
                        log.tab_event("tab_close", m["params"]["targetId"][:8], "", "")
        except (OSError, FileNotFoundError) as e:
            log.ev("chrome_down", error=type(e).__name__)
        except Exception as e:  # unexpected — log and keep the loop alive
            log.ev("ws_error", error=f"{type(e).__name__}: {e}"[:200])
        await asyncio.sleep(5)


async def _focus_loop(log: Log):
    last = None
    fails = 0
    disabled = False
    while True:
        if not disabled:
            url, title = _active_tab()
            if url is None:
                fails += 1
                if fails == 3:
                    disabled = True
                    log.ev(
                        "focus_unavailable",
                        note="osascript blocked in this context; tab events only",
                    )
            else:
                fails = 0
                if (url, title) != last:
                    log.ev("focus", url=url, title=title)
                    last = (url, title)
        await asyncio.sleep(POLL_FOCUS_S)


async def _amain(seconds: float | None):
    log = Log()
    log.ev("spike_start", pid=os.getpid())
    stop = asyncio.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    tasks = [asyncio.create_task(_ws_loop(log)), asyncio.create_task(_focus_loop(log))]
    if seconds:
        await asyncio.sleep(seconds)
    else:
        await stop.wait()
    for t in tasks:
        t.cancel()
    log.ev("spike_stop")


def logger_main(seconds: float | None = None):
    try:
        asyncio.run(_amain(seconds))
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- control ---


def start() -> str:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".venv", "bin", "python3"))
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{py}</string><string>-m</string><string>seeress.watcher</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{WATCH_DIR}/logger.log</string>
  <key>StandardErrorPath</key><string>{WATCH_DIR}/logger.log</string>
</dict></plist>
"""
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    plist.write_text(content)
    r = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
    if r.returncode != 0:
        return f"launchctl load failed: {r.stderr.strip()}"
    return (
        f"watcher running (LaunchAgent {LABEL}, survives reboot + Chrome restart).\n"
        f"log  -> {WATCH_DIR}/spike-*.jsonl\n"
        f"stop -> seeress watch-stop"
    )


def stop() -> str:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    if plist.exists():
        plist.unlink()
    return "watcher stopped"


# --------------------------------------------------------------- analysis ---


def _domain(url: str) -> str:
    url = url.strip()
    if not url or url.startswith(("chrome", "about:", "data:", "devtools")):
        return ""
    try:
        rest = url.split("://", 1)[1]
    except IndexError:
        return ""
    host = rest.split("/", 1)[0]
    return host.lower().removeprefix("www.") or ""


def _load_events(hours: float | None) -> list[dict[str, Any]]:
    events = []
    cutoff = time.time() - hours * 3600 if hours else 0
    for f in sorted(WATCH_DIR.glob("spike-*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and time.mktime(time.strptime(ev["ts"], "%Y-%m-%dT%H:%M:%S")) < cutoff:
                continue
            events.append(ev)
    return events


def _parse_ts(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))


def analyze(hours: float | None = None, no_llm: bool = False) -> str:
    from .brain import endpoint_for

    events = _load_events(hours)
    if not events:
        return "no watcher data yet — run `seeress watch-start` first"

    # -- domain visit sequence: prefer focus stream, fall back to nav events --
    focus = [e for e in events if e["kind"] == "focus" and _domain(e.get("url", ""))]
    seq_domains: list[str] = []
    domain_seconds: dict[str, float] = {}
    if focus:
        for i, e in enumerate(focus):
            d = _domain(e["url"])
            seq_domains.append(d)
            if i + 1 < len(focus):
                gap = min(_parse_ts(focus[i + 1]["ts"]) - _parse_ts(e["ts"]), FOCUS_GAP_CAP_S)
                domain_seconds[d] = domain_seconds.get(d, 0.0) + max(gap, 0.0)
    else:
        for e in events:
            if e["kind"] in ("nav", "tab_open") and _domain(e.get("url", "")):
                seq_domains.append(_domain(e["url"]))

    # collapse consecutive repeats (one "visit" per domain run)
    visits: list[str] = []
    for d in seq_domains:
        if not visits or visits[-1] != d:
            visits.append(d)
    visit_counts: dict[str, int] = {}
    for d in visits:
        visit_counts[d] = visit_counts.get(d, 0) + 1

    # ordered behavior chains for the brain: n-grams alone miss varied rituals
    # (same event source as seq_domains, but keeping timestamps)
    ts_domains: list[tuple[str, str]] = []
    if focus:
        for e in focus:
            ts_domains.append((e["ts"], _domain(e["url"])))
    else:
        for e in events:
            if e["kind"] in ("nav", "tab_open"):
                d = _domain(e.get("url", ""))
                if d:
                    ts_domains.append((e["ts"], d))
    hour_chains: dict[str, list[str]] = {}
    for ts, d in ts_domains:
        hr = ts[:13]  # YYYY-MM-DDTHH
        chain = hour_chains.setdefault(hr, [])
        if not chain or chain[-1] != d:
            chain.append(d)

    # -- repeated multi-step sequences (n-grams over the visit sequence) --
    seqs: dict[tuple[str, ...], int] = {}
    for n in range(2, 7):
        for i in range(len(visits) - n + 1):
            gram = tuple(visits[i : i + n])
            if len(set(gram)) >= 2:  # multi-domain only = a *flow* shape
                seqs[gram] = seqs.get(gram, 0) + 1
    repeated = sorted(
        ((g, c) for g, c in seqs.items() if c >= 3),
        key=lambda gc: gc[1] * len(gc[0]),
        reverse=True,
    )[:15]

    # -- tab churn --
    nav_counts: dict[str, int] = {}
    for e in events:
        if e["kind"] == "nav":
            d = _domain(e.get("url", ""))
            if d:
                nav_counts[d] = nav_counts.get(d, 0) + 1

    window = f"last {int(hours)}h" if hours else "all recorded time"
    lines = [
        f"# Seeress spike analysis — {window}",
        f"_generated {time.strftime('%Y-%m-%d %H:%M')}, "
        f"{len(events)} events, focus stream: {'yes' if focus else 'no (tab events only)'}_\n",
    ]

    lines.append("## Time per domain (focus, hours:h)")
    for d, s in sorted(domain_seconds.items(), key=lambda kv: -kv[1])[:12]:
        lines.append(f"- {s / 3600:5.1f}h  {d}")
    lines.append("\n## Most-visited domains (visit runs)")
    for d, c in sorted(visit_counts.items(), key=lambda kv: -kv[1])[:12]:
        lines.append(f"- {c:3d}×  {d}")
    lines.append("\n## Repeated multi-step sequences (flow candidates)")
    if repeated:
        for g, c in repeated:
            lines.append(f"- **{c}×**  {' → '.join(g)}")
    else:
        lines.append("- none found yet (need ≥3 repeats of a multi-domain sequence)")
    lines.append("\n## Tab churn (navigations per domain)")
    for d, c in sorted(nav_counts.items(), key=lambda kv: -kv[1])[:8]:
        lines.append(f"- {c:3d} navs  {d}")
    # the ordered timeline — this is where varied-but-real rituals live
    lines.append("\n## Ordered behavior chain (full, consecutive repeats collapsed)")
    full_chain: list[str] = []
    for _, d in ts_domains:
        if not full_chain or full_chain[-1] != d:
            full_chain.append(d)
    if full_chain:
        shown = " → ".join(full_chain[:150])
        lines.append(shown + (" …(truncated)" if len(full_chain) > 150 else ""))
    lines.append("\n## Hour-by-hour chains (ordered, collapsed)")
    for hr in sorted(hour_chains):
        chain = hour_chains[hr]
        lines.append(f"- **{hr[11:]}h** ({len(chain):3d} moves): " + " → ".join(chain[:40]))
    report = "\n".join(lines)

    out = WATCH_DIR / f"analysis-{time.strftime('%Y%m%d-%H%M')}.md"
    out.write_text(report + "\n")

    if no_llm:
        return report + f"\n\n[saved {out}]"

    # -- local brain: turn the mining into flow proposals (nothing leaves) --
    # The report is one person's activity. None says "the page could not be checked",
    # which makes the guard allow a model on this machine and nothing else.
    ep = endpoint_for("client", None)
    from .brain import chat

    prompt = (
        "You are analyzing one person's browser activity mining report "
        "to find AUTOMATABLE repetitive workflows.\n\n"
        f"{report}\n\n"
        "The 'Ordered behavior chain' and 'Hour-by-hour chains' sections show the "
        "actual TIME-ORDERED sequence of domains visited. Use them: a ritual can be "
        "repetitive even when its exact order varies between rounds (e.g. the same "
        "cluster of tool-sites visited together many times, even if interleaved with "
        "one-off research sites). Clusters that recur across multiple hours count as "
        "repetition even if no rigid A→B→C loop appears.\n\n"
        "Propose up to 5 concrete flows. For each: (1) name, (2) what it does, "
        "(3) the domain sequence it follows, (4) why it is valuable "
        "(estimate times/week saved), (5) a confidence 1-5 that it is truly "
        "repetitive rather than coincidental. Only propose flows grounded in the "
        "ordered data above. Be blunt: if the data shows no real repetitive work, "
        "say so."
    )
    print("[seeress] asking local brain (first call after idle may take 2-4 min)...", flush=True)
    proposals = chat(ep, [{"role": "user", "content": prompt}], timeout=600)
    with open(out, "a") as f:
        f.write("\n## Flow proposals (local brain)\n\n" + proposals + "\n")
    return report + "\n\n## Flow proposals (local brain)\n\n" + proposals + f"\n\n[saved {out}]"


if __name__ == "__main__":
    logger_main()
