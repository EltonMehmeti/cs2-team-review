# CS2 Team Review

Reads your team's CS2 demo files after a match and stores what happened in a
small database: rounds, kills, damage, grenades, flashes, bomb events, money
and where everyone stood. Later phases add team stats, AI reports and play
suggestions (see `PLAN.md`).

It only reads demo files from disk. It never touches the running game, so it
cannot cause FPS drops or anti-cheat problems. Run it after you finish
playing.

## Setup (once)

1. Install Python 3.11 or newer.
2. In this folder:

   ```
   pip install -r requirements.txt
   ```

3. Copy `config.example.toml` to `config.toml` and put in the steamID64 of
   your five players. You find a steamID64 by pasting a Steam profile link
   into a site such as steamid.io. It is a 17-digit number starting with 7656.

## Use

1. Download a demo. On FACEIT: open the match room and click **Watch Demo**.
   You get a `.dem.zst` file. Only one of you needs to download it; the file
   contains all ten players. FACEIT keeps demos for about 30 days.
2. Put the file into the `inbox` folder. No need to unpack it.
3. Run:

   ```
   python -m cs2review.ingest
   ```

   You should see one line per demo, ending with
   `check OK: matches the demo's own scoreboard`.
4. Look at the result:

   ```
   python -m cs2review.check
   ```

   This prints the scoreboard and the list of rounds.

The database is the file `data/cs2review.sqlite`. Open it with any SQLite
viewer, for example "DB Browser for SQLite".

## What "check OK" means

The demo contains the game's own final scoreboard. The tool rebuilds kills,
deaths, assists, damage and the score from single events and compares them.
If they agree, the parsing can be trusted.

If you see `CHECK FAILED`, run:

```
python -m cs2review.debug_rounds inbox/<file name>
```

and send the output to whoever maintains the tool. The usual cause is a
knife round or a restart that was counted as a real round.

## Commands

| Command | What it does |
|---|---|
| `python -m cs2review.ingest` | read every demo in `inbox/` |
| `python -m cs2review.ingest --force` | read again, replacing stored matches |
| `python -m cs2review.ingest some.dem` | read specific files or folders |
| `python -m cs2review.check` | scoreboard and check of the latest match |
| `python -m cs2review.check --all` | short check of every match |
| `python -m cs2review.check --list` | list stored matches |
| `python -m cs2review.debug_rounds X` | show the raw round events of a demo |

## Words used in the database

| Word | Meaning |
|---|---|
| tick | one snapshot of the game; 64 per second |
| `time_s` | seconds since the round went live (end of freeze time) |
| side | T or CT |
| team | A or B. A is the team that starts on T. The letter stays when sides swap. `matches.us_team` tells which one is yours |
| place | the game's own name for a map area, such as `Jungle` or `BombsiteA` |
| `played` | 0 for a round that never went live (a surrender in freeze time) |

## Tables

`matches`, `players`, `rounds`, `player_rounds` (money and gear per round),
`kills`, `damages`, `grenades`, `blinds`, `bomb_events`, `positions` (every
living player once per second), `map_places`. Every column is described in
`cs2review/db.py`.

Two example questions you can already ask with SQL:

```sql
-- where do our players die first?
SELECT victim_place, COUNT(*) AS deaths
FROM kills k JOIN players p
  ON p.match_id = k.match_id AND p.steamid = k.victim_steamid
WHERE p.is_us = 1
  AND k.id IN (SELECT MIN(id) FROM kills GROUP BY match_id, round)
GROUP BY victim_place ORDER BY deaths DESC;

-- who flashes teammates most?
SELECT p.name, SUM(g.teammates_blinded) AS teammates_blinded
FROM grenades g JOIN players p
  ON p.match_id = g.match_id AND p.steamid = g.thrower_steamid
WHERE g.type = 'flash' AND p.is_us = 1
GROUP BY p.name ORDER BY 2 DESC;
```

## Known limits

- Tested on a Valve matchmaking demo. FACEIT demos are supported by design
  (knife rounds and restarts are skipped) but your first real demos are the
  real test. That is why the check exists.
- `grenades.land_place` is approximate: it is the nearest area where a
  player has stood. It gets better as you add demos of the same map.
- The demo does not contain the match date. `matches.file_time` is the
  file's modification time.
- CS:GO demos are not supported.
- After updating the code, delete `data/cs2review.sqlite` and run the ingest
  again. The database is rebuilt from the demos in seconds.

## Handing the rest to an AI agent

`PLAN.md` is the full build plan and `CLAUDE.md` holds the working rules.
Open this folder with a coding agent and say: "Read PLAN.md and CLAUDE.md,
then do Phase 1.5." Do the phases in order.
