"""
Central configuration for the league + Club Friendlies live poller.

SCOPE: leagues (config.LEAGUES) and Club Friendlies for those leagues'
clubs (config.FRIENDLIES) only -- no World Cup, no other internationals.
The old standalone World Cup scraper.py has been removed entirely.

ARCHITECTURE:
365Scores is the sole live data source — fixtures discovery, score, status,
and structured events (goal/card/sub) all come from it.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# MongoDB
MONGO_URI = os.environ.get("MONGO_URI", "")
MONGO_DB = os.environ.get("MONGO_DB", "clashdb")
# NOTE: renamed from "fixtures" -> "games". Both the World Cup poller and
# the new multi-league scraper (leagues_scraper.py) now write into the
# same "games" collection. Override with MONGO_COLLECTION env var if you
# need to point at a different collection name.
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "games")

# Rust API
FANCLASH_API = os.environ.get("FANCLASH_API", "https://clash-api-m5mr.onrender.com/api")

# 365Scores
THREESIXTYFIVE_BASE_URL = "https://webws.365scores.com"
THREESIXTYFIVE_APP_TYPE_ID = 5
THREESIXTYFIVE_LANG_ID = 1
THREESIXTYFIVE_USER_COUNTRY_ID = 413
THREESIXTYFIVE_TIMEZONE = "Africa/Nairobi"

# Polling
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
SCRAPE_DAYS_AHEAD = 7

# ============================================================
# UNIFIED SCRAPE WINDOW (leagues AND friendlies, both anchored on today)
# ============================================================
# Single shared window size, in days, used by BOTH
# scrape_all_leagues_window() and scrape_all_friendlies_window() in
# leagues_scraper.py -- both now anchor strictly on "today"
# (datetime.now(UTC).date()), no reference-date creep/high-water-mark
# heuristics. Replaces the old split of REFERENCE_WINDOW_DAYS (13, for
# leagues) vs FRIENDLIES_WINDOW_DAYS (10, for friendlies) with one
# number applied identically to both, per explicit request: "13 days
# for both friendlies and leagues, referenced from today, all of them."
SCRAPE_WINDOW_DAYS = 13

# ============================================================
# LEAGUE-BASED FIXTURES (leagues_scraper.py)
# ============================================================
# 365Scores competitionId for each league/cup, derived from each
# competition's canonical 365scores.com URL slug (the trailing number
# in e.g. .../league/premier-league-7 is the competitionId). These are
# stable in practice but 365Scores has been known to reshuffle IDs
# across seasons -- if a league starts returning 0 games, re-derive the
# id from https://www.365scores.com/football/league/<slug>-<id> and
# update this dict.
#
# `prefix` is used to build each document's matchId, e.g. "epl_4627864",
# mirroring the existing wc26_<gameId> convention used for the World Cup.
LEAGUES = {
    "epl": {
        "competition_id": 7,
        "name": "Premier League",
        "prefix": "epl",
    },
    "seriea": {
        "competition_id": 17,
        "name": "Serie A",
        "prefix": "seriea",
    },
    "ucl": {
        "competition_id": 572,
        "name": "UEFA Champions League",
        "prefix": "ucl",
    },
    "europa": {
        "competition_id": 573,
        "name": "UEFA Europa League",
        "prefix": "europa",
    },
    "facup": {
        "competition_id": 8,
        "name": "FA Cup",
        "prefix": "facup",
    },
    "community_shield": {
        "competition_id": 10,
        "name": "Community Shield",
        "prefix": "community_shield",
    },
}

# ============================================================
# PRIORITY-LEAGUE ROLLING WINDOW (legacy -- see SCRAPE_WINDOW_DAYS)
# ============================================================
# Instead of anchoring the scrape window on "now" (a dead zone until the
# priority leagues' seasons actually start), this used to anchor on a
# reference date that starts here and creeps forward by one day per real
# calendar day (see FixtureStore.advance_reference_date_if_needed).
#
# NO LONGER USED by scrape_all_leagues_window() -- leagues now anchor on
# today directly, same as friendlies, per SCRAPE_WINDOW_DAYS above. Left
# in place only because mongo_store.FixtureStore.get_reference_date() /
# advance_reference_date_if_needed() still reference these constants;
# harmless dead weight now that nothing calls those methods from the
# scrape path.
REFERENCE_DATE_DEFAULT = "2026-08-13"
REFERENCE_WINDOW_DAYS = 13

# Order matters here -- this is also the priority order used when
# building the "top of feed" response (EPL first, down to Community
# Shield). Qualifying rounds are excluded regardless of league.
PRIORITY_LEAGUE_ORDER = ["epl", "ucl", "europa", "facup", "community_shield"]

# ============================================================
# CLUB FRIENDLIES (EPL / Serie A clubs only) -- leagues_scraper.py
# ============================================================
# IMPORTANT: 365Scores does NOT have a separate competitionId per
# parent league for friendlies -- there is no "EPL friendlies" or
# "Serie A friendlies" id to fetch. Every club's friendly worldwide
# (6000+ clubs) is pooled into ONE competition, "Club Friendlies",
# verified live at:
#   https://www.365scores.com/football/league/club-friendlies-321
# If this ever starts returning 0 games, re-derive the id from that
# URL's trailing number, same as the LEAGUES dict above.
FRIENDLIES_COMPETITION_ID = 321

# Because the id above is a single global "everyone's friendlies"
# bucket, leagues_scraper.py fetches it whole and then narrows it down
# to only fixtures where at least one side is a club in these name
# lists (case-insensitive substring match against 365Scores'
# homeCompetitor/awayCompetitor "name" field -- see
# threesixtyfive.filter_games_by_club_names). Several aliases are
# listed per club since 365Scores' display name doesn't always match
# the club's full/common English name (e.g. "Man City" vs "Manchester
# City", "Spurs" vs "Tottenham", "Inter" vs "Internazionale").
#
# Rosters below reflect the CONFIRMED 2026-27 line-ups as of the top
# of this season (verified July 2026, not carried over from the
# 2025-26 season):
#   EPL: Coventry City, Ipswich Town, Hull City promoted, replacing
#        Wolves, Burnley, West Ham (relegated).
#   Serie A: Venezia, Frosinone, Monza promoted, replacing Cremonese,
#        Hellas Verona, Pisa (relegated).
# Re-check both lists every close season -- a stale list here silently
# drops that club's friendlies (false negative) or, if a promoted/
# relegated club shares a name fragment with an existing entry,
# wrongly includes friendlies that belong to a different division
# entirely (false positive).
EPL_CLUB_NAMES = [
    "Liverpool",
    "Arsenal",
    "Manchester City",
    "Man City",
    "Chelsea",
    "Newcastle",
    "Aston Villa",
    "Nottingham Forest",
    "Nott'm Forest",
    "Brighton",
    "Bournemouth",
    "Fulham",
    "Crystal Palace",
    "Everton",
    "Brentford",
    "Manchester United",
    "Man Utd",
    "Man United",
    "Tottenham",
    "Spurs",
    "Sunderland",
    "Leeds",
    "Coventry",
    "Ipswich",
    "Hull City",
    "Hull",
]

SERIEA_CLUB_NAMES = [
    "Atalanta",
    "Bologna",
    "Cagliari",
    "Como",
    "Fiorentina",
    "Frosinone",
    "Genoa",
    "Inter",
    "Internazionale",
    "Juventus",
    "Juve",
    "Lazio",
    "Lecce",
    "AC Milan",
    "Milan",
    "Monza",
    "Napoli",
    "Parma",
    "Roma",
    "Sassuolo",
    "Torino",
    "Udinese",
    "Venezia",
]

# Default friendlies window: 10 days starting 2026-07-26, per the
# pre-season friendly slate requested when this was set up. Override
# with --start-date/--days on leagues_scraper.py's CLI for any later
# window once this one has passed. Only used by scrape_friendlies()
# (the fixed-date-range variant) -- the rolling scrape_friendlies_window()
# now defaults to SCRAPE_WINDOW_DAYS from today instead.
FRIENDLIES_DEFAULT_START_DATE = "2026-07-25"
FRIENDLIES_DEFAULT_RANGE_DAYS = 10

# Rolling window size (in days) for scrape_friendlies_window() /
# scrape_all_friendlies_window(). Superseded by SCRAPE_WINDOW_DAYS above
# for the automatic path (poller.py's _trigger_rescrape and
# leagues_scraper.py's default no-flags run both now pass
# SCRAPE_WINDOW_DAYS explicitly) -- kept only as the fallback default on
# scrape_friendlies_window()'s days_ahead parameter for direct/manual
# calls that don't pass one.
FRIENDLIES_WINDOW_DAYS = 10

FRIENDLIES = {
    "epl_friendlies": {
        "competition_id": FRIENDLIES_COMPETITION_ID,
        "name": "Premier League Club Friendlies",
        "prefix": "epl_friendly",
        "club_names": EPL_CLUB_NAMES,
    },
    "seriea_friendlies": {
        "competition_id": FRIENDLIES_COMPETITION_ID,
        "name": "Serie A Club Friendlies",
        "prefix": "seriea_friendly",
        "club_names": SERIEA_CLUB_NAMES,
    },
}
