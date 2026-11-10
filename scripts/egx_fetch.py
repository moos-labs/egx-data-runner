#!/usr/bin/env python3
"""Test EGX-native data sources (egxpy / Borsa) for POUL OHLC history.

Key question: can either source deliver historical OHLC - in particular the
ex-dividend-day OPEN, which Yahoo reports incorrectly (always = prior close)?
Reference opens from Investing (real): see EX_DAYS below.
Always writes results/verdict.json and results/report.md, whatever happens.
"""
import json, math, os, signal, subprocess, sys, traceback
from datetime import date

os.makedirs("results", exist_ok=True)

MERGER_FACTOR = 1789351397 / 479001600  # 3.7355854281, Sep 2026 POUL merger
EX_DAYS = [
    {"ex_date": "2019-07-15", "investing_open": 5.05},
    {"ex_date": "2021-05-18", "investing_open": 2.85},
    {"ex_date": "2024-10-22", "investing_open": 12.30},
    {"ex_date": "2026-04-27", "investing_open": 34.80},
]
LATEST_REF = {"date": "2026-11-09", "close": 9.99}

VERDICT = {"reference": {"latest_close": LATEST_REF, "ex_day_opens_investing": EX_DAYS,
                         "merger_factor": MERGER_FACTOR},
           "sources": {}}


class Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise Timeout("attempt timed out")


def run_with_timeout(fn, seconds=420):
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(seconds)
    try:
        return fn()
    finally:
        signal.alarm(0)


def pip_install(spec, timeout=600):
    """Tolerant pip install; returns (ok, tail_of_output)."""
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", spec],
                           capture_output=True, text=True, timeout=timeout)
        tail = (p.stdout + p.stderr).strip().splitlines()
        return p.returncode == 0, "\n".join(tail[-6:])
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def close(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


def judge_open(source_open, investing_open, prior_close):
    """Classify one ex-day open against Investing / prior close, merger-aware."""
    if source_open is None:
        return "absent"
    if close(source_open, investing_open, 0.02):
        return "matches Investing"
    if prior_close is not None and close(source_open, prior_close, 0.005):
        return "matches prior close (Yahoo-style, not a real open)"
    adj = source_open * MERGER_FACTOR
    if close(adj, investing_open, 0.05):
        return "matches Investing after merger adjustment (source series is merger-adjusted)"
    if prior_close is not None and close(adj, prior_close, 0.02):
        return "matches prior close after merger adjustment"
    return "differs from both Investing and prior close"


def analyse_frame(df, source_name):
    """df: pandas DataFrame with Date/Open/High/Low/Close(/Volume). Returns info dict."""
    import pandas as pd
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    df = df.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
    df.to_csv(f"results/{source_name}_poul.csv", index=False)
    info = {"rows": int(len(df)),
            "date_range": [str(df["Date"].iloc[0]), str(df["Date"].iloc[-1])],
            "columns": [str(c) for c in df.columns]}
    last = df.iloc[-1]
    info["latest"] = {"date": str(last["Date"]), "close": float(last["Close"]),
                      "matches_reference_9_99": bool(close(float(last["Close"]), 9.99, 0.02))}
    rows = []
    for ex in EX_DAYS:
        exd = date.fromisoformat(ex["ex_date"])
        hit = df[df["Date"] == exd]
        before = df[df["Date"] < exd]
        prior_close = float(before.iloc[-1]["Close"]) if len(before) else None
        s_open = float(hit.iloc[0]["Open"]) if len(hit) and "Open" in df.columns else None
        s_close = float(hit.iloc[0]["Close"]) if len(hit) else None
        rows.append({"ex_date": ex["ex_date"], "investing_open": ex["investing_open"],
                     "source_open": s_open, "source_close": s_close,
                     "prior_close_in_source": prior_close,
                     "verdict": judge_open(s_open, ex["investing_open"], prior_close)})
    info["ex_day_opens"] = rows
    return info


# ---------------------------------------------------------------- egxpy ----
def normalise(df):
    """Best-effort normalisation of a library DataFrame to Date/Open/.../Close."""
    import pandas as pd
    if df is None:
        return None
    if isinstance(df, dict):  # some APIs return {ticker: DataFrame}
        for v in df.values():
            r = normalise(v)
            if r is not None:
                return r
        return None
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    df = df.copy()
    if isinstance(df.index, pd.DatetimeIndex) or str(df.index.name).lower() in ("date", "datetime"):
        df = df.reset_index()
    colmap = {}
    for c in df.columns:
        lc = str(c).strip().lower()
        if lc in ("date", "datetime", "index", "time"):
            colmap[c] = "Date"
        elif lc in ("open", "o"):
            colmap[c] = "Open"
        elif lc in ("high", "h"):
            colmap[c] = "High"
        elif lc in ("low", "l"):
            colmap[c] = "Low"
        elif lc in ("close", "c", "price"):
            colmap[c] = "Close"
        elif lc in ("volume", "vol", "v"):
            colmap[c] = "Volume"
    df = df.rename(columns=colmap)
    if "Date" not in df.columns or "Close" not in df.columns:
        return None
    keep = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume") if c in df.columns]
    return df[keep]


def try_egxpy():
    src = {"install_attempts": [], "fetch_attempts": []}
    mod = None
    try:
        import egxpy  # noqa
        mod = egxpy
        src["install_attempts"].append({"spec": "(preinstalled by requirements-egx)", "ok": True})
    except ImportError:
        for spec in ("egxpy", "git+https://github.com/mmabas77/egxpy"):
            ok, tail = pip_install(spec)
            src["install_attempts"].append({"spec": spec, "ok": ok, "output_tail": tail})
            if ok:
                try:
                    import importlib
                    mod = importlib.import_module("egxpy")
                    break
                except ImportError as e:
                    src["install_attempts"].append({"spec": spec, "ok": False,
                                                    "output_tail": f"installed but import failed: {e}"})
    if mod is None:
        src["status"] = "failed"
        src["failure"] = "egxpy could not be installed/imported from PyPI or its GitHub repo"
        return src
    try:
        from egxpy import download as dl
    except Exception:
        dl = mod
    fns = [n for n in ("get_OHLCV_data", "get_EGXdata", "get_EGX_intraday_data") if hasattr(dl, n)]
    src["available_functions"] = fns
    if not fns:
        src["status"] = "failed"
        src["failure"] = f"egxpy imported but exposes none of the expected download functions (dir: {[a for a in dir(dl) if not a.startswith('_')][:40]})"
        return src

    import pandas as pd
    windows = [("2018-01-01", "2026-11-10"),
               ("2019-06-01", "2019-08-15"), ("2021-04-01", "2021-06-15"),
               ("2024-09-01", "2024-11-15"), ("2026-03-01", "2026-05-15"),
               ("2026-08-01", "2026-11-10")]
    frames = []
    for fname in fns:
        fn = getattr(dl, fname)
        for (s, e) in windows:
            for symarg in (["POUL"], "POUL"):
                label = f"{fname}({symarg!r}, {s}, {e})"
                try:
                    out = run_with_timeout(lambda: fn(symarg, s, e), 420)
                    ndf = normalise(out)
                    src["fetch_attempts"].append({"call": label, "ok": ndf is not None,
                                                  "rows": (int(len(ndf)) if ndf is not None else 0)})
                    if ndf is not None:
                        frames.append(ndf)
                except Exception as ex:
                    src["fetch_attempts"].append({"call": label, "ok": False,
                                                  "error": f"{type(ex).__name__}: {str(ex)[:300]}"})
            if frames and fname == fns[0]:
                break  # first function + widest window worked; enough
        if frames:
            break
    if not frames:
        src["status"] = "failed"
        src["failure"] = "egxpy installed and imported, but every fetch call failed or returned no usable OHLC frame (see fetch_attempts)"
        return src
    merged = pd.concat(frames, ignore_index=True)
    info = analyse_frame(merged, "egxpy")
    src.update(info)
    src["status"] = "ok"
    return src


# ---------------------------------------------------------------- borsa ----
def try_borsa():
    import requests
    src = {"install_attempts": [], "api_surface": None, "demo_service": {}}
    # 1) install / import check
    try:
        import borsa  # noqa
        src["install_attempts"].append({"spec": "(preinstalled by requirements-egx)", "ok": True})
        have_pkg = True
    except ImportError:
        ok, tail = pip_install("git+https://github.com/7ashraf/borsa")
        src["install_attempts"].append({"spec": "git+https://github.com/7ashraf/borsa", "ok": ok, "output_tail": tail})
        have_pkg = ok
    if have_pkg:
        try:
            import borsa.fetchers as bf
            fns = [a for a in dir(bf) if not a.startswith("_")]
            src["package_fetchers"] = fns
        except Exception as e:
            src["package_fetchers_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        # The Fetcher protocol is fetch_quote-only (verified in repo source);
        # record whether ANY history-like callable exists in the package.
        hist_like = []
        try:
            import borsa, pkgutil
            for m in pkgutil.iter_modules(borsa.__path__):
                hist_like.append(m.name)
            src["package_modules"] = hist_like
        except Exception as e:
            src["package_modules_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    src["api_surface"] = ("REST API exposes quotes only (/v1/quote/{symbol}, /v1/quotes, "
                          "/v1/stocks, /v1/health); Fetcher protocol = fetch_quote. "
                          "No history/OHLC endpoint exists in the published API.")
    # 2) public demo service - prove what it can and cannot do
    base = "https://borsa.ashh.me"
    for path in ("/v1/health", "/v1/quote/POUL", "/demo/quote/POUL",
                 "/v1/history/POUL", "/v1/ohlc/POUL", "/v1/candles/POUL"):
        try:
            r = requests.get(base + path, timeout=30, headers={"User-Agent": "egx-data-runner/1.0"})
            body = r.text[:400]
            src["demo_service"][path] = {"status_code": r.status_code, "body_head": body}
        except Exception as e:
            src["demo_service"][path] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    src["status"] = "no_history"
    src["failure"] = ("Borsa is a quote service by design: no historical OHLC endpoint or "
                      "library function exists, so it cannot provide ex-day opens or any "
                      "price history. Demo-service probe results above show its live surface.")
    return src


# ----------------------------------------------------------------- main ----
def write_report():
    lines = ["# EGX data sources for POUL — verdict", "",
             "Reference (Yahoo chart API): latest close 2026-11-09 = **9.99**. "
             "Reference ex-day opens (Investing, real opens): " +
             ", ".join(f"{e['ex_date']} → {e['investing_open']}" for e in EX_DAYS) + ".", ""]
    for name, s in VERDICT["sources"].items():
        lines.append(f"## {name} — {s.get('status')}")
        if s.get("rows"):
            lines.append(f"- Rows: {s['rows']} · Range: {s['date_range'][0]} → {s['date_range'][1]}")
            lt = s.get("latest", {})
            lines.append(f"- Latest: {lt.get('date')} close {lt.get('close')} "
                         f"(matches Yahoo 9.99: {lt.get('matches_reference_9_99')})")
        if s.get("ex_day_opens"):
            lines += ["", "| Ex-date | Investing open | Source open | Prior close (source) | Verdict |",
                      "|---|---|---|---|---|"]
            for r in s["ex_day_opens"]:
                lines.append(f"| {r['ex_date']} | {r['investing_open']} | {r['source_open']} | "
                             f"{r['prior_close_in_source']} | {r['verdict']} |")
            lines.append("")
        if s.get("api_surface"):
            lines.append(f"- API surface: {s['api_surface']}")
        if s.get("failure"):
            lines.append(f"- Failure/finding: {s['failure']}")
        lines.append("")
    txt = "\n".join(lines)
    with open("results/report.md", "w") as f:
        f.write(txt)
    print(txt)


def main():
    for name, fn in (("egxpy", try_egxpy), ("borsa", try_borsa)):
        print(f"===== {name} =====", flush=True)
        try:
            VERDICT["sources"][name] = fn()
        except Exception:
            VERDICT["sources"][name] = {"status": "failed",
                                        "failure": "unhandled exception:\n" + traceback.format_exc()[-1500:]}
        with open("results/verdict.json", "w") as f:
            json.dump(VERDICT, f, indent=2, default=str)
    write_report()


if __name__ == "__main__":
    main()
