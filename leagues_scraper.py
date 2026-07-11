"""
League-based fixture scraper. THE single scraper module for this service --
the old World Cup-only scraper.py has been removed; everything (manual
full/round scrapes and the automatic rolling-window scrape) lives here now.

Generates fixtures for multiple competitions -- Premier League, Serie A,
UEFA Champions League, UEFA Europa League, FA Cup, Community Shield --
from 365Scores and stores them all in config.MONGO_COLLECTION (defaults
to "games").

poller.py's _trigger_rescrape() calls scrape_all_leagues_window() below
(a rolling config.SCRAPE_DAYS_AHEAD-day window, 7 by default) -- both on
the reactive trigger fired right after a match is archived/finalized, and
on the twice-daily scheduled backstop. The full-season and single-round
functions (scrape_league_fixtures / scrape_one_round) are kept for
manual/CLI use -- e.g. seeding a brand-new league for the first time --
but the poller itself only ever calls the *_window variants.

Usage:
    # Scrape every configured league, full fixture list each:
    python leagues_scraper.py --league all

    # Scrape just one league:
    python leagues_scraper.py --league epl

    # Scrape ONLY the next (or current) round of the Premier League --
    # useful right as a season is starting up and you only want Round 1
    # in the database instead of the entire fixture list:
    python leagues_scraper.py --league epl --round-only

    # Same, but pin to a specific round number instead of "whichever
    # round is next":
    python leagues_scraper.py --league epl --round-only --round-num 1

    # Rolling window (what the poller runs automatically) -- only writes
    # fixtures that kick off within the next N days, so a league whose
    # season hasn't started yet simply upserts nothing:
    python leagues_scraper.py --league epl --window
    python leagues_scraper.py --league all --window --days-ahead 7
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import sys
from typing import Optional

from dotenv import load_dotenv

from mongo_store import FixtureStore
from sources import threesixtyfive
import config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worldcup_poller.leagues_scraper")


def _status_to_internal(status_text: str) -> str:
    text = (status_text or "").strip().lower()
    if text in ("finished", "ft", "ended", "full-time", "aet", "pen"):
        return "completed"

    live_patterns = (r"\blive\b", r"\b1st half\b", r"\b2nd half\b", r"\bht\b", r"\bhalftime\b", r"\bin progress\b")
    if any(re.search(pattern, text) for pattern in live_patterns):
        return "live"

    return "upcoming"


def _is_qualifying_round(game: dict, fallback_league_name: str) -> bool:
    """True if this fixture belongs to a qualifying/preliminary round
    rather than the main competition -- e.g. "UEFA Champions League
    Qualifiers - 2nd Round" or "FA Cup - Qualifying Rounds - Extra
    Preliminary Round". Checked against 365Scores' own
    competitionDisplayName first (most reliable), falling back to the
    configured league name and roundName in case a feed doesn't put the
    qualifier marker in the competition name itself."""
    text = " ".join(filter(None, [
        game.get("competitionDisplayName"),
        fallback_league_name,
        game.get("roundName"),
    ])).lower()
    return "qualif" in text or "preliminary" in text


def _parse_kickoff(start_time_raw: Optional[str]) -> datetime.datetime:
    now = datetime.datetime.now(datetime.timezone.utc)
    if not start_time_raw:
        return now
    try:
        return datetime.datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return now


def _upsert_games(store: FixtureStore, games: list[dict], league_key: str) -> int:
    """Upsert a list of raw 365Scores game dicts for one league into the
    games collection, tagging each with leagueKey/roundNum/roundName so
    they can be queried back out by round later."""
    league_cfg = config.LEAGUES[league_key]
    prefix = league_cfg["prefix"]
    league_name = league_cfg["name"]

    upserted = 0
    for game in games:
        game_id = str(game.get("id"))
        home_team = (game.get("homeCompetitor") or {}).get("name", "Unknown")
        away_team = (game.get("awayCompetitor") or {}).get("name", "Unknown")
        home_competitor_id = (game.get("homeCompetitor") or {}).get("id")
        away_competitor_id = (game.get("awayCompetitor") or {}).get("id")
        competition_id = game.get("competitionId")
        comp_name = game.get("competitionDisplayName") or league_name
        kickoff = _parse_kickoff(game.get("startTime"))
        status = _status_to_internal(game.get("statusText", ""))
        match_id = f"{prefix}_{game_id}"

        store.upsert_fixture(
            match_id=match_id,
            threesixtyfive_game_id=game_id,
            home_team=home_team,
            away_team=away_team,
            home_competitor_id=home_competitor_id,
            away_competitor_id=away_competitor_id,
            competition_id=competition_id,
            kickoff_utc=kickoff,
            status=status,
            competition_name=comp_name,
            odds=game.get("odds", {}),
            league_key=league_key,
            round_num=game.get("roundNum"),
            round_name=game.get("roundName"),
            group_num=game.get("groupNum"),
            group_name=game.get("groupName"),
        )
        upserted += 1
        logger.info(
            "Upserted %s: %s vs %s [%s] round=%s kickoff=%s (%s)",
            match_id, home_team, away_team, status,
            game.get("roundNum"), kickoff.strftime("%Y-%m-%d %H:%M"), comp_name,
        )

    return upserted


def scrape_league_fixtures(store: FixtureStore, league_key: str) -> int:
    """Fetch and upsert ALL fixtures 365Scores returns for one league."""
    if league_key not in config.LEAGUES:
        raise ValueError(f"Unknown league key: {league_key!r}. Known: {list(config.LEAGUES)}")

    league_cfg = config.LEAGUES[league_key]
    competition_id = league_cfg["competition_id"]

    logger.info(
        "Fetching %s fixtures from 365Scores (competitionId=%s) ...",
        league_cfg["name"], competition_id,
    )
    games = threesixtyfive.fetch_games_by_competition([competition_id])

    if games is None:
        logger.error("fetch_games_by_competition(%s) returned None -- network/API error", competition_id)
        return 0

    logger.info("365Scores returned %d raw games for %s", len(games), league_cfg["name"])

    if not games:
        logger.warning(
            "0 games returned for %s (competitionId=%s) -- the id may be stale, "
            "re-derive it from the league's 365scores.com URL slug.",
            league_cfg["name"], competition_id,
        )
        return 0

    return _upsert_games(store, games, league_key)


def scrape_all_leagues(store: FixtureStore) -> dict[str, int]:
    """Scrape every league in config.LEAGUES. Returns {league_key: count}."""
    results: dict[str, int] = {}
    for league_key in config.LEAGUES:
        try:
            results[league_key] = scrape_league_fixtures(store, league_key)
        except Exception as exc:
            logger.error("Scrape failed for league=%s: %s", league_key, exc)
            results[league_key] = 0
    return results


def scrape_one_round(store: FixtureStore, league_key: str, round_num: Optional[int] = None) -> int:
    """Fetch a league's fixtures and upsert only ONE round of them.

    If round_num is None, picks the "current" round automatically: the
    lowest roundNum among fixtures that haven't finished yet (i.e. the
    next round to be played -- which, if the season hasn't started yet,
    is simply Round 1 / the opening round).
    """
    if league_key not in config.LEAGUES:
        raise ValueError(f"Unknown league key: {league_key!r}. Known: {list(config.LEAGUES)}")

    league_cfg = config.LEAGUES[league_key]
    competition_id = league_cfg["competition_id"]

    logger.info(
        "Fetching %s fixtures from 365Scores (competitionId=%s) to find one round ...",
        league_cfg["name"], competition_id,
    )
    games = threesixtyfive.fetch_games_by_competition([competition_id])

    if not games:
        logger.warning(
            "0 games returned for %s (competitionId=%s) -- cannot determine round.",
            league_cfg["name"], competition_id,
        )
        return 0

    if round_num is None:
        # Candidate rounds are ones with at least one not-yet-finished game.
        not_finished = [g for g in games if not threesixtyfive.is_game_finished(g)]
        pool = not_finished or games
        round_nums = [g.get("roundNum") for g in pool if g.get("roundNum") is not None]
        if not round_nums:
            logger.warning("No roundNum field present on any fetched game for %s.", league_cfg["name"])
            return 0
        round_num = min(round_nums)
        logger.info("Auto-selected round %s for %s (earliest unplayed round).", round_num, league_cfg["name"])

    round_games = [g for g in games if g.get("roundNum") == round_num]
    logger.info(
        "%d/%d games belong to round %s for %s",
        len(round_games), len(games), round_num, league_cfg["name"],
    )

    if not round_games:
        return 0

    return _upsert_games(store, round_games, league_key)


# ============================================================
# ROLLING WINDOW SCRAPE (used automatically by poller.py)
# ============================================================

def scrape_league_fixtures_window(
    store: FixtureStore,
    league_key: str,
    days_ahead: int = 7,
    reference_override: Optional[datetime.datetime] = None,
) -> int:
    """Fetch a league's fixtures and upsert only the ones landing in the
    next `days_ahead` days (plus anything already in progress). This is
    what keeps a league 'topped up' on a rolling basis instead of writing
    the whole season at once -- mirrors config.SCRAPE_DAYS_AHEAD, the
    same window convention the World Cup poller used to use.

    If the earliest upcoming kickoff for this league is further out than
    the window, nothing is upserted -- the season hasn't started yet, so
    there's nothing in-window to write. This function is called on every
    reactive rescrape (after a match completes) and on the poller's
    twice-daily backstop, so once the season's first fixture falls inside
    the window it starts showing up on its own -- no separate "has the
    season started" check is needed.

    reference_override: if given, use this as the window anchor directly
    instead of the last_kickoff/earliest_kickoff heuristic below. This is
    how scrape_all_leagues_window() pins every priority league to the
    single shared, slowly-advancing reference date instead of each league
    guessing its own anchor independently.
    """
    if league_key not in config.LEAGUES:
        raise ValueError(f"Unknown league key: {league_key!r}. Known: {list(config.LEAGUES)}")

    league_cfg = config.LEAGUES[league_key]
    competition_id = league_cfg["competition_id"]

    logger.info(
        "Fetching %s fixtures from 365Scores (competitionId=%s) for %d-day window ...",
        league_cfg["name"], competition_id, days_ahead,
    )

    games = threesixtyfive.fetch_games_by_competition([competition_id])

    if games is None:
        logger.error("fetch_games_by_competition(%s) returned None -- network/API error", competition_id)
        return 0

    if not games:
        logger.warning(
            "0 games returned for %s (competitionId=%s) -- either the id is "
            "stale or the season hasn't been scheduled by 365Scores yet.",
            league_cfg["name"], competition_id,
        )
        return 0

    now = datetime.datetime.now(datetime.timezone.utc)

    if reference_override is not None:
        reference = reference_override
        logger.info(
            "%s: using shared reference date %s (override, not the per-league heuristic)",
            league_cfg["name"], reference.strftime("%Y-%m-%d"),
        )
    else:
        # Reference point for the window -- NOT always "today". Two cases
        # where we deliberately anchor elsewhere:
        #
        # 1. This league has already been seeded ahead of "today" by a
        #    previous windowed scrape (last_kickoff > now). Anchor on that
        #    high-water mark instead of "now" so each poller trigger (the
        #    twice-daily backstop, or the reactive rescrape after a match
        #    archives) advances the window forward from where the LAST scrape
        #    left off, rather than re-checking the same near-term slice
        #    relative to today over and over.
        #
        # 2. Nothing usable has been scraped yet and the league's own season
        #    hasn't started within the normal `days_ahead` window (e.g. it's
        #    July and the EPL doesn't open until August 21). Anchor on the
        #    competition's own earliest upcoming kickoff -- its real start
        #    date -- so the opening batch of fixtures gets seeded as soon as
        #    365Scores has published them, instead of silently doing nothing
        #    until we happen to be within `days_ahead` of kickoff.
        last_kickoff = store.get_latest_kickoff_for_league(league_key)

        if last_kickoff and last_kickoff > now:
            reference = last_kickoff
            logger.info(
                "%s: continuing rolling window from last-scraped kickoff %s (not today)",
                league_cfg["name"], reference.strftime("%Y-%m-%d %H:%M"),
            )
        else:
            not_finished = [g for g in games if not threesixtyfive.is_game_finished(g)]
            earliest_kickoff = min(
                (_parse_kickoff(g.get("startTime")) for g in not_finished),
                default=None,
            )
            if earliest_kickoff and earliest_kickoff > now + datetime.timedelta(days=days_ahead):
                reference = earliest_kickoff
                logger.info(
                    "%s hasn't started yet -- anchoring window on season start %s instead of today",
                    league_cfg["name"], reference.strftime("%Y-%m-%d %H:%M"),
                )
            else:
                reference = now

    cutoff = reference + datetime.timedelta(days=days_ahead)

    in_window = []
    skipped_before_window = 0
    skipped_qualifier = 0
    for g in games:
        kickoff = _parse_kickoff(g.get("startTime"))

        # Lower bound: must be at or after `reference`, UNLESS the game
        # is actually live right now -- catches a match that kicked off
        # slightly before the window but is still being played. This is
        # deliberately narrower than "not finished", which is true for
        # every upcoming fixture regardless of date and was the original
        # bug: it let e.g. an Aug-8 FA Cup qualifier through a window
        # anchored on Aug 13, because "upcoming" games are never
        # "finished" no matter how far away their kickoff is.
        is_live_now = _status_to_internal(g.get("statusText", "")) == "live"
        if kickoff < reference and not is_live_now:
            skipped_before_window += 1
            continue

        # Upper bound: must not kick off after the window ends.
        if kickoff > cutoff:
            continue

        # Exclude qualifying/preliminary rounds regardless of date.
        if _is_qualifying_round(g, league_cfg["name"]):
            skipped_qualifier += 1
            continue

        in_window.append(g)

    if skipped_before_window or skipped_qualifier:
        logger.info(
            "%s: filtered out %d fixture(s) before the window and %d qualifying-round fixture(s)",
            league_cfg["name"], skipped_before_window, skipped_qualifier,
        )

    if not in_window:
        logger.info(
            "%s: no non-qualifier fixtures within the %d-day window from %s.",
            league_cfg["name"], days_ahead, reference.strftime("%Y-%m-%d"),
        )
        return 0

    logger.info(
        "%s: %d/%d fixtures fall within the %d-day window from %s (qualifiers excluded)",
        league_cfg["name"], len(in_window), len(games), days_ahead, reference.strftime("%Y-%m-%d"),
    )
    return _upsert_games(store, in_window, league_key)


def scrape_all_leagues_window(store: FixtureStore, days_ahead: int = 7) -> dict[str, int]:
    """Windowed version of scrape_all_leagues -- this is what poller.py
    calls automatically (on the reactive post-match-completion trigger
    and the twice-daily scheduled backstop). Returns {league_key: count}.

    Every call through here advances the shared reference date by one
    day, but only on the FIRST call of any given real calendar day --
    store.advance_reference_date_if_needed() enforces that atomically,
    so it doesn't matter whether this fires once or a dozen times today
    (e.g. several matches completing back-to-back)."""
    reference = store.advance_reference_date_if_needed()
    logger.info(
        "scrape_all_leagues_window: shared reference date = %s, window = %d days",
        reference.strftime("%Y-%m-%d"), days_ahead,
    )

    results: dict[str, int] = {}
    for league_key in config.LEAGUES:
        try:
            results[league_key] = scrape_league_fixtures_window(
                store, league_key, days_ahead, reference_override=reference
            )
        except Exception as exc:
            logger.error("Windowed scrape failed for league=%s: %s", league_key, exc)
            results[league_key] = 0
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape league-based fixtures into the games collection.")
    parser.add_argument(
        "--league",
        default="all",
        choices=["all"] + list(config.LEAGUES.keys()),
        help="Which league to scrape (default: all).",
    )
    parser.add_argument(
        "--round-only",
        action="store_true",
        help="Only fetch a single round instead of the full fixture list. Only valid with a single --league (not 'all').",
    )
    parser.add_argument(
        "--round-num",
        type=int,
        default=None,
        help="Pin --round-only to a specific roundNum instead of auto-selecting the next unplayed round.",
    )
    parser.add_argument(
        "--window",
        action="store_true",
        help="Only fetch fixtures within --days-ahead days (rolling window), same behavior the poller runs automatically.",
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=config.SCRAPE_DAYS_AHEAD,
        help=f"Window size in days for --window (default: {config.SCRAPE_DAYS_AHEAD}, from config.SCRAPE_DAYS_AHEAD).",
    )
    args = parser.parse_args()

    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        logger.error("MONGO_URI environment variable is required")
        sys.exit(1)

    store = FixtureStore(mongo_uri)
    try:
        if args.round_only:
            if args.league == "all":
                logger.error("--round-only requires a specific --league (e.g. --league epl), not 'all'.")
                sys.exit(1)
            count = scrape_one_round(store, args.league, round_num=args.round_num)
            logger.info("Round scrape complete: %d games upserted into '%s' collection.", count, config.MONGO_COLLECTION)
        elif args.window:
            if args.league == "all":
                results = scrape_all_leagues_window(store, days_ahead=args.days_ahead)
                total = sum(results.values())
                logger.info("Windowed all-league scrape complete: %s (total=%d) into '%s' collection.", results, total, config.MONGO_COLLECTION)
            else:
                count = scrape_league_fixtures_window(store, args.league, days_ahead=args.days_ahead)
                logger.info("Windowed scrape complete: %d games upserted into '%s' collection.", count, config.MONGO_COLLECTION)
        elif args.league == "all":
            results = scrape_all_leagues(store)
            total = sum(results.values())
            logger.info("All-league scrape complete: %s (total=%d) into '%s' collection.", results, total, config.MONGO_COLLECTION)
        else:
            count = scrape_league_fixtures(store, args.league)
            logger.info("Scrape complete: %d games upserted into '%s' collection.", count, config.MONGO_COLLECTION)
    except Exception as exc:
        logger.error("Scrape failed: %s", exc)
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()