#!/usr/bin/env python3
"""Prometheus exporter for the FuzeInfra self-closed automation loop.

Three workflows open a GitHub issue or PR carrying an autofix label when
something breaks and a coding agent is expected to fix it
(argo-outofsync-autofix.yml, grafana-crit-fix.yml, alertmanager-fuze.yml).
Whether those loops ever actually close was, until this exporter, only
answerable by hand-running GitHub label queries. See
docs/self-heal-loop-metrics.md.

Stdlib only, on purpose: this is a low-QPS poller, and a dependency-free image
is the difference between a 60 MB layer and a rebuild every time a pinned
library gets a CVE.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GITHUB_API = "https://api.github.com"
PER_PAGE = 100
USER_AGENT = "fuzeinfra-selfheal-metrics-exporter"

# A hand-built OpenerDirector, NOT build_opener(): build_opener() merges in
# ALL of urllib's default handlers (including FileHandler/FTPHandler) unless
# one of the passed handlers subclasses them, so build_opener(HTTPSHandler())
# still opens file:// — verified: it happily read()s a local file. Adding
# exactly these handlers (no File/FTP/Data/Gopher) means the scheme lookup
# itself fails for anything but http(s), regardless of what URL string a bug
# ever constructs. HTTPHandler is required even though every URL we build is
# https: HTTPErrorProcessor's error path is hardcoded to look up 'http' in
# the handler table ("https is not different than http", per its source).
_HTTPS_ONLY_OPENER = urllib.request.OpenerDirector()
for _handler in (
    urllib.request.ProxyHandler(),  # honors HTTP(S)_PROXY, same as urlopen()'s default opener
    urllib.request.HTTPHandler(),
    urllib.request.HTTPSHandler(),
    urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.HTTPRedirectHandler(),
    urllib.request.HTTPErrorProcessor(),
    urllib.request.UnknownHandler(),
):
    _HTTPS_ONLY_OPENER.add_handler(_handler)

DEFAULT_LABEL_SYSTEM_MAP = {
    "argo-autofix": "argocd",
    "crit-autofix": "loki",
    "alertmanager-autofix": "alertmanager",
}

# A loop closes in hours-to-days, not milliseconds; the buckets are spaced to
# keep histogram_quantile meaningful across that whole range.
LATENCY_BUCKETS = (
    300.0, 900.0, 1800.0, 3600.0, 7200.0, 21600.0,
    43200.0, 86400.0, 172800.0, 604800.0,
)

log = logging.getLogger("selfheal-metrics")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("invalid %s, using default %s", name, default)
        return default


def _label_system_map() -> dict[str, str]:
    raw = os.environ.get("LABEL_SYSTEM_MAP", "").strip()
    if not raw:
        return dict(DEFAULT_LABEL_SYSTEM_MAP)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise ValueError("expected a flat object of label -> system strings")
        return parsed
    except (ValueError, TypeError) as exc:
        log.warning("invalid LABEL_SYSTEM_MAP (%s), using defaults", exc)
        return dict(DEFAULT_LABEL_SYSTEM_MAP)


REPO = os.environ.get("GITHUB_REPO", "FuzeOne/FuzeInfra")
POLL_INTERVAL_SECONDS = _env_int("POLL_INTERVAL_SECONDS", 600)
LISTEN_PORT = _env_int("LISTEN_PORT", 9109)
LABEL_SYSTEM_MAP = _label_system_map()


class RateLimited(Exception):
    """GitHub refused the call for rate-limit/permission reasons."""


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    ).timestamp()


def _get_json(path: str) -> list[dict]:
    url = f"{GITHUB_API}{path}"
    if not url.startswith("https://api.github.com/"):
        raise ValueError(f"refusing to open a non-GitHub-API URL: {url!r}")
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        },
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        # _HTTPS_ONLY_OPENER, not urlopen(): no FileHandler is registered, so
        # a file:// URL 404s at the handler-lookup stage instead of reading a
        # local path, regardless of what url ends up being.
        with _HTTPS_ONLY_OPENER.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise RateLimited(f"HTTP {exc.code} on {path}") from exc
        raise


def _fetch_labeled_items(label: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"labels": label, "state": "all", "per_page": PER_PAGE, "page": page}
        )
        batch = _get_json(f"/repos/{REPO}/issues?{query}")
        items.extend(batch)
        if len(batch) < PER_PAGE:
            return items
        page += 1


def classify(items: list[dict]) -> dict:
    """Reduce one label's items to the four loop counters plus latencies.

    A resolution is a merged PR carrying the label. The /issues list endpoint
    already returns pull_request.merged_at for PR rows, so no per-PR follow-up
    GET is needed (verified against the live API on 2026-09-14) — which matters
    because the unauthenticated rate limit is 60 requests/hour.

    manual_close is best-effort: a closed non-PR issue whose state_reason is
    not "completed". GitHub stamps "completed" when an issue is closed by the
    merged PR that references it, so excluding it keeps a loop that DID
    self-close from being counted as both a resolution and a manual close. A
    human closing an issue as "completed" by hand is therefore missed; exact
    issue<->PR cross-linking is out of scope for v1.
    """
    counts = {"opened": 0, "resolved": 0, "open": 0, "manual_close": 0}
    latencies: list[float] = []
    resolved_items: list[dict] = []

    for item in items:
        counts["opened"] += 1
        pr = item.get("pull_request") or {}
        merged_at = _parse_ts(pr.get("merged_at"))

        if item.get("state") == "open":
            counts["open"] += 1
        if merged_at is not None:
            counts["resolved"] += 1
            created_at = _parse_ts(item.get("created_at"))
            seconds = max(0.0, merged_at - created_at) if created_at else 0.0
            latencies.append(seconds)
            resolved_items.append(
                {
                    "number": item.get("number"),
                    "url": item.get("html_url"),
                    "title": item.get("title"),
                    "resolution_seconds": round(seconds, 1),
                }
            )
        elif item.get("state") == "closed" and not pr:
            if item.get("state_reason") != "completed":
                counts["manual_close"] += 1

    return {"counts": counts, "latencies": latencies, "resolved_items": resolved_items}


class Collector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: dict[str, dict] = {}
        self._last_success = 0.0
        self._scrape_errors = 0
        self._seen_resolved: dict[str, set[int]] = {}
        self._seeded = False

    def poll_once(self) -> None:
        fresh: dict[str, dict] = {}
        # Build the whole cycle before committing: a partial snapshot would make
        # the counters dip for whichever label failed, which reads on a dashboard
        # as loops being un-resolved.
        for label, system in LABEL_SYSTEM_MAP.items():
            result = classify(_fetch_labeled_items(label))
            bucket = fresh.setdefault(
                system,
                {"counts": {k: 0 for k in ("opened", "resolved", "open", "manual_close")},
                 "latencies": [], "resolved_items": []},
            )
            for key, value in result["counts"].items():
                bucket["counts"][key] += value
            bucket["latencies"].extend(result["latencies"])
            bucket["resolved_items"].extend(result["resolved_items"])

        self._emit_new_resolutions(fresh)
        with self._lock:
            self._snapshot = fresh
            self._last_success = time.time()

    def _emit_new_resolutions(self, fresh: dict[str, dict]) -> None:
        # The first successful poll seeds the seen-set silently. Logging every
        # historical resolution on each pod start would republish the same events
        # into Loki on every restart and rollout.
        for system, data in fresh.items():
            numbers = {i["number"] for i in data["resolved_items"]}
            known = self._seen_resolved.get(system, set())
            if self._seeded:
                for item in data["resolved_items"]:
                    if item["number"] in known:
                        continue
                    print(json.dumps({
                        "event": "selfheal_resolved",
                        "system": system,
                        "issue_number": item["number"],
                        "url": item["url"],
                        "title": item["title"],
                        "resolution_seconds": item["resolution_seconds"],
                    }), flush=True)
            self._seen_resolved[system] = known | numbers
        self._seeded = True

    def record_error(self) -> None:
        with self._lock:
            self._scrape_errors += 1

    def render(self) -> str:
        with self._lock:
            snapshot = self._snapshot
            last_success = self._last_success
            errors = self._scrape_errors

        out: list[str] = []
        gauges = (
            ("opened", "Autofix issues/PRs ever opened for this system."),
            ("resolved", "Autofix loops closed by a merged agent-authored PR."),
            ("open", "Autofix issues/PRs still open."),
            ("manual_close", "Autofix issues closed without a merged PR resolving them."),
        )
        for key, help_text in gauges:
            name = f"fuzeinfra_selfheal_loop_{key}_total"
            out.append(f"# HELP {name} {help_text}")
            # Gauge, not counter, despite the _total suffix: every value is
            # recomputed from GitHub (the source of truth) each poll rather than
            # incremented in-process, so it survives a restart and must not be
            # read through rate()/increase().
            out.append(f"# TYPE {name} gauge")
            for system in sorted(snapshot):
                out.append(f'{name}{{system="{system}"}} {snapshot[system]["counts"][key]}')

        hist = "fuzeinfra_selfheal_loop_resolution_seconds"
        out.append(f"# HELP {hist} Time from autofix item creation to the resolving PR merge.")
        out.append(f"# TYPE {hist} histogram")
        for system in sorted(snapshot):
            latencies = snapshot[system]["latencies"]
            for bound in LATENCY_BUCKETS:
                cumulative = sum(1 for v in latencies if v <= bound)
                out.append(f'{hist}_bucket{{system="{system}",le="{bound}"}} {cumulative}')
            out.append(f'{hist}_bucket{{system="{system}",le="+Inf"}} {len(latencies)}')
            out.append(f'{hist}_sum{{system="{system}"}} {sum(latencies)}')
            out.append(f'{hist}_count{{system="{system}"}} {len(latencies)}')

        ts = "fuzeinfra_selfheal_loop_last_scrape_success_timestamp_seconds"
        out.append(f"# HELP {ts} Unix time of the last poll in which every label was fetched.")
        out.append(f"# TYPE {ts} gauge")
        out.append(f"{ts} {last_success}")

        err = "fuzeinfra_selfheal_loop_scrape_errors_total"
        out.append(f"# HELP {err} Poll cycles abandoned because the GitHub API call failed.")
        out.append(f"# TYPE {err} counter")
        out.append(f"{err} {errors}")

        return "\n".join(out) + "\n"


COLLECTOR = Collector()


def poll_loop() -> None:
    while True:
        try:
            COLLECTOR.poll_once()
        except RateLimited as exc:
            COLLECTOR.record_error()
            log.warning("skipping poll cycle: %s", exc)
        except Exception as exc:  # noqa: BLE001 - a poller must never crash-loop
            COLLECTOR.record_error()
            log.warning("skipping poll cycle: %s: %s", type(exc).__name__, exc)
        time.sleep(POLL_INTERVAL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.startswith("/metrics"):
            body = COLLECTOR.render().encode("utf-8")
            content_type = "text/plain; version=0.0.4; charset=utf-8"
        elif self.path.startswith("/healthz"):
            body = b"ok\n"
            content_type = "text/plain; charset=utf-8"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        # Prometheus scrapes every 15s; an access line per scrape would drown the
        # selfheal_resolved events this pod exists to publish into Loki.
        return


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    log.info(
        "repo=%s interval=%ss labels=%s authenticated=%s",
        REPO,
        POLL_INTERVAL_SECONDS,
        sorted(LABEL_SYSTEM_MAP),
        bool(os.environ.get("GITHUB_TOKEN", "").strip()),
    )
    threading.Thread(target=poll_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
