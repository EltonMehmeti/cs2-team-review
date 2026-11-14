"""Phase 8: the web scout. Recent plays from articles and videos, always with a link.

    python -m cs2review.scout --map de_mirage          # meant to run once a week
    python -m cs2review.scout                          # every map we have played

The model searches the web with Claude's web search tool (a limited number
of searches per map and per run), and returns findings as JSON. The code
keeps a finding only if:
  * its URL is one the search tool actually returned (no invented links),
  * the URL opens (unless [scout] check_urls = false),
  * it is about the right map, side T or CT, site A, B or none, with steps.
Kept findings go into the playbook with source_kind = 'web' and
needs_review = 1. Web cards have no lineup commands.

Search results are untrusted text: the prompt tells the model to ignore any
instruction in them, and the code only keeps short, checked fields.

Cost on 2026-11-14: $10 per 1,000 searches plus tokens. Every run logs its
searches and cost in llm_calls and stops at [llm] max_cost_usd_per_run.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Engine, delete, text

from .config import Config, load_config
from .db import Playbook, SchemaMismatchError, get_engine
from .llm import LLM, LLMError, extract_json
from .playbook import card_id, load_statuses
from .zones import load_zones

PROMPTS = Path(__file__).parent / "prompts"
MAX_STEPS = 12
MAX_TEXT = 300  # characters per title or step: search results are untrusted text


class Finding(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str
    url: str
    published: str | None = None
    map: str
    side: str
    site: str | None = None
    steps: list[str]
    video_timestamp: str | None = None


def _norm(url: str) -> str:
    return url.strip().rstrip("/").replace("http://", "https://", 1).lower()


def url_opens(url: str, opener=urllib.request.urlopen, timeout: float = 10.0) -> bool:
    """True when the page answers with a 2xx or 3xx status."""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0 (cs2review link check)"})
    try:
        with opener(request, timeout=timeout) as response:
            return 200 <= getattr(response, "status", 200) < 400
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return False


def check_findings(answer_text: str, map_name: str, result_urls: list[dict],
                   url_check: Callable[[str], bool] | None = None) -> tuple[list[dict], list[str]]:
    """Parse the model's findings and keep only the checked ones. Returns (kept, reasons for dropping)."""
    try:
        raw = json.loads(extract_json(answer_text))
    except (json.JSONDecodeError, LLMError) as exc:
        return [], [f"the answer is not valid JSON ({exc})"]
    items = raw.get("findings", []) if isinstance(raw, dict) else []
    known = {_norm(u["url"]): u for u in result_urls if u.get("url")}
    kept, dropped = [], []
    for i, item in enumerate(items, 1):
        try:
            f = Finding.model_validate(item)
        except ValidationError as exc:
            dropped.append(f"finding {i}: wrong shape ({exc.errors()[0]['msg']})")
            continue
        reason = None
        if not f.url.lower().startswith(("https://", "http://")):
            reason = "the URL is not a web link"
        elif _norm(f.url) not in known:
            reason = "the URL was not in the search results"
        elif f.map != map_name:
            reason = f"about {f.map}, not {map_name}"
        elif f.side not in ("T", "CT"):
            reason = f"side '{f.side}' is not T or CT"
        elif f.site not in (None, "A", "B"):
            reason = f"site '{f.site}' is not A or B"
        elif not f.title.strip() or not any(s.strip() for s in f.steps):
            reason = "no title or no steps"
        elif url_check is not None and not url_check(f.url):
            reason = "the URL does not open"
        if reason:
            dropped.append(f"finding {i} ({f.url}): {reason}")
            continue
        # keep only a real date (YYYY-MM-DD...); the search tool's page_age is free text like "2 weeks ago"
        published = f.published if f.published and len(f.published) >= 10 and f.published[:4].isdigit() else None
        kept.append({
            "title": f.title.strip()[:MAX_TEXT], "url": f.url.strip(), "published": published[:10] if published else None,
            "map": f.map, "side": f.side, "site": f.site,
            "steps": [s.strip()[:MAX_TEXT] for s in f.steps if s.strip()][:MAX_STEPS],
            "video_timestamp": (f.video_timestamp or None) and f.video_timestamp[:10],
        })
    return kept, dropped


def build_prompts(map_name: str, max_searches: int, recent_days: int, today: str) -> tuple[str, str]:
    system = ((PROMPTS / "scout_system.txt").read_text(encoding="utf-8")
              .replace("{recent_days}", str(recent_days)).replace("{today}", today).replace("{map}", map_name))
    user = ((PROMPTS / "scout_user.txt").read_text(encoding="utf-8")
            .replace("{map}", map_name).replace("{max_searches}", str(max_searches)))
    return system, user


def save_web_cards(engine: Engine, cfg: Config, findings: list[dict]) -> list[str]:
    """Add (or refresh) web cards in the playbook. Returns their ids."""
    statuses = load_statuses(cfg.playbook_status_path)
    ids = []
    with engine.begin() as conn:
        for f in findings:
            signature = f"{f['map']}|{f['side']}|{f['site'] or '?'}|web:{f['url']}"
            pid = card_id(signature)
            zones = load_zones(f["map"], cfg.maps_dir)
            last_update = zones.last_map_update if zones else None
            card = {
                "title": f["title"], "label": "web",
                "steps": [{"do": step} for step in f["steps"]],
                "examples": [],
                "source": {"url": f["url"], "published": f["published"], "video_timestamp": f["video_timestamp"]},
                "evidence": {"seen": 0, "won": 0, "window_days": None},
                "lineup_verified": False,
            }
            conn.execute(delete(Playbook).where(Playbook.id == pid))
            conn.execute(Playbook.__table__.insert().values(
                id=pid, signature=signature, map=f["map"], side=f["side"], site=f["site"], title=f["title"],
                source_kind="web", seen=0, won=0, first_seen=f["published"], last_seen=f["published"],
                difficulty=0, status=statuses.get(pid, "new"),
                stale=bool(last_update and f["published"] and f["published"] < last_update),
                needs_review=True, card=json.dumps(card, ensure_ascii=False)))
            ids.append(pid)
    return ids


def scout_map(engine: Engine, cfg: Config, llm: LLM, map_name: str, max_searches: int,
              today: str | None = None, url_check: Callable[[str], bool] | None = None) -> dict:
    """Search, check and store findings for one map."""
    today = today or date.today().isoformat()
    system, user = build_prompts(map_name, max_searches, cfg.scout.recent_days, today)
    result = llm.search(system, user, max_searches=max_searches, tool_type=cfg.scout.tool_type,
                        price_per_search=cfg.scout.price_per_search, purpose="scout", key=map_name)
    kept, dropped = check_findings(result.text, map_name, result.urls, url_check)
    ids = save_web_cards(engine, cfg, kept)
    return {"map": map_name, "searches": result.searches, "cost_usd": result.cost_usd,
            "kept": kept, "ids": ids, "dropped": dropped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find recent plays on the web, with links (weekly).")
    ap.add_argument("--map", action="append", help="map to scout (repeat for several; default: every map we played)")
    ap.add_argument("--no-url-check", action="store_true", help="do not open the URLs before keeping findings")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config(args.config)
    try:
        engine = get_engine(cfg.db_path)
        llm = LLM.from_config(cfg.llm, engine, provider="anthropic")
    except SchemaMismatchError as exc:
        print(exc)
        return 1
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1
    with engine.connect() as conn:
        maps = args.map or [r[0] for r in conn.execute(text("SELECT DISTINCT map FROM matches WHERE source_kind = 'own' ORDER BY map"))]
    if not maps:
        print("No maps to scout. Pass --map de_mirage or ingest a match first.")
        return 1
    check = None if (args.no_url_check or not cfg.scout.check_urls) else url_opens
    left = cfg.scout.max_searches_per_run
    total_cost = 0.0
    for map_name in maps:
        if left <= 0:
            print(f"Stopped before {map_name}: the run's search limit ({cfg.scout.max_searches_per_run}) is used up.")
            break
        try:
            out = scout_map(engine, cfg, llm, map_name, min(cfg.scout.max_searches_per_map, left), url_check=check)
        except LLMError as exc:
            print(f"{map_name}: FAILED: {exc}")
            break
        left -= out["searches"]
        total_cost += out["cost_usd"]
        print(f"{map_name}: {len(out['kept'])} finding(s) kept, {out['searches']} search(es), ${out['cost_usd']:.4f}")
        for f, pid in zip(out["kept"], out["ids"]):
            print(f"  {pid}  {f['side']} {f['site'] or '-'}  {f['title']}\n         {f['url']}")
        for reason in out["dropped"]:
            print(f"  dropped: {reason}")
    print(f"\nTotal ${total_cost:.4f}. New web cards need review: python -m cs2review.playbook list --status new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
