# CS2 Team Review: working rules for agents

Read `PLAN.md` before changing anything. It holds the goals, the phases, the
exact rules for every metric, and the facts already verified about the demo
parser. This file is the short version of how to work here.

## What this project is

A post-match review tool for one five-player CS2 team on FACEIT. It parses
demo files into SQLite, computes team metrics, and uses an LLM to write
reports and suggest plays. Phases 1 (demo -> SQLite + self-check), 2 (team
metrics and round flags) and 3 (round summaries and match digest) are done.
Phases 4 (LLM match report), 5 (trends and question agent), 6 (play
detection), 7 (playbook) and 8 (web scout) are built and wait for the
owner's API key, webhook, real matches, pro demos and zone review. Phase 1.5 waits for
the owner's FACEIT demos.

## Hard rules

- The tool only reads `.dem` files after a match. Never add anything that
  reads game memory, injects, overlays, sends inputs, or runs during a match.
- No scraping of hltv.org. No Steam or FACEIT passwords. Keys come from
  environment variables.
- Numbers in reports come from SQL or Python, never from the LLM counting.
- Every AI claim cites round numbers or a source URL.
- Keep `python -m cs2review.check --all` passing. It compares our numbers
  with the game's own scoreboard.

## Commands

```
pip install -r requirements.txt
python -m cs2review.ingest              # demos in inbox/ -> data/cs2review.sqlite
python -m cs2review.check --all         # self-check of every match
python -m cs2review.metrics --rebuild   # recompute Phase 2 metrics (ingest does it too)
python -m cs2review.metrics --buy-histogram   # tune buy-type thresholds
python -m cs2review.digest MATCH_ID --pretty  # the JSON the LLM reads
python -m cs2review.report MATCH_ID --provider manual [--post]  # LLM match report
python -m cs2review.trends --map de_mirage --last 10 [--report]  # trends across matches
python -m cs2review.ask "question"      # question agent (needs ANTHROPIC_API_KEY)
python -m cs2review.zones               # check / draft maps/<map>.toml zone files
python -m cs2review.mapview de_mirage   # PNG to review a zone file
python -m cs2review.plays build|list|review|coverage|show   # play detection
python -m cs2review.ingest --kind pro   # pro demos from pro_inbox/
python -m cs2review.playbook build|list|show|status|beat-us  # play cards
python -m cs2review.scout --map de_mirage   # weekly web scout (needs ANTHROPIC_API_KEY)
python tests/eval_ask.py                # agent evaluation (real model, ~1 cent)
python -m cs2review.debug_rounds X.dem  # raw round events of one demo
python tests/test_rounds.py             # unit tests (no demo needed)
python tests/test_metrics.py            # Phase 2 unit tests (no demo needed)
python tests/test_summaries.py          # Phase 3 unit tests (no demo needed)
python tests/test_report.py             # Phase 4 unit tests (fake model, no network)
python tests/test_trends_ask.py         # Phase 5 unit tests (synthetic database)
python tests/test_plays.py              # Phase 6 unit tests
python tests/test_playbook.py           # Phase 7 unit tests
python tests/test_scout.py              # Phase 8 unit tests (fake search)
python tests/test_integration.py        # end-to-end (needs tests/data/test_demo.dem)
```

Get the public test demo once:

```
git clone --depth 1 https://github.com/LaihoE/demoparser tmp_demoparser
# copy tmp_demoparser/src/parser/test_demo.dem to tests/data/test_demo.dem
```

## How to work

- One phase at a time, in the order of `PLAN.md`. Finish its "Done when"
  list, then update the status table at the top of `PLAN.md`.
- When unsure how the parser behaves, write a short script and run it on the
  test demo. Do not guess. Add what you learn to section 4 of `PLAN.md`.
- The database is a cache that can be rebuilt from the demos. Change tables
  freely, bump `SCHEMA_VERSION` in `db.py`, re-ingest. No migrations.
- Tests are plain functions with asserts that run with and without pytest.
  Unit tests build small DataFrames by hand and never need a demo.
- Write simple code and explain CS2 terms in comments: the owner is a junior
  engineer who is new to CS2 analytics.
- Demo files, the database and `config.toml` are git-ignored. Never commit
  them. `data/manual_plays.json` (the IGL's labels) and
  `data/playbook_status.json` (card statuses) are user data: never delete them.
- Ask the owner for the inputs listed in section 9 of `PLAN.md` instead of
  inventing them (Steam IDs, map pool, zone files, API keys).
- Tests never call a real model. Use `FakeProvider` from `llm.py`.

## Code layout

```
cs2review/config.py        settings from config.toml
cs2review/unpack.py        decompress and validate demo files
cs2review/rounds.py        choose the real rounds of a match
cs2review/parse.py         demo -> DataFrames (the core of Phase 1)
cs2review/places.py        area names for grenade landing points
cs2review/db.py            SQLAlchemy models, SCHEMA_VERSION, save/load helpers
cs2review/ingest.py        CLI
cs2review/check.py         CLI: scoreboard, self-check, KAST and opening duels
cs2review/metrics.py       Phase 2: team_rounds, player_round_stats, kill flags
cs2review/summaries.py     Phase 3: round summaries, match digest, pydantic models
cs2review/digest.py        CLI: print or save a match digest
cs2review/llm.py           the only module that calls a model (anthropic, manual, fake)
cs2review/report.py        CLI: LLM match report, validated and rendered by code
cs2review/notify.py        post to a Discord webhook
cs2review/trends.py        CLI: trends across matches
cs2review/trend_report.py  weekly trend report (LLM, evidence paths checked)
cs2review/ask.py           CLI: question agent, read-only run_sql
cs2review/prompts/         prompt text files (never put prompts inside functions)
cs2review/zones.py         zone files per map (maps/<map>.toml)
cs2review/mapview.py       CLI: map PNG coloured by zone
cs2review/plays.py         CLI: play features and labels, manual review
cs2review/playbook.py      CLI: play cards from opponents and pro demos
cs2review/scout.py         CLI: web scout, links checked against search results
maps/                      zone files (owner-reviewed; de_mirage is a draft)
tests/fixtures.py          synthetic 3-match database for tests
cs2review/debug_rounds.py  CLI: raw round events
```
