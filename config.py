"""
Central configuration for the World Cup live poller.

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
WORLD_CUP_COMPETITION_IDS = [5930]
SCRAPE_DAYS_AHEAD = 7

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