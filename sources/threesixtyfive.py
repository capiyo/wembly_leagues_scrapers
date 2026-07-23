"""
365Scores API client for league, friendly, and World Cup/international data.
Fetches: fixtures, live scores, events, lineups, statistics, and commentary.

NOTE: This is the league-scraper's copy of the module and had drifted from
the World Cup poller's copy -- it was missing the shared _fetch_play_by_play_raw
helper and, worse, fetch_commentary() hardcoded "team": None instead of
resolving CompetitorNum the way fetch_match_events() already did. Both are
fixed below by mirroring the World Cup version's structure exactly. The
league-only additions (fetch_games_by_date_range, filter_games_by_club_names)
are kept as-is since the World Cup version doesn't need them.
"""

from __future__ import annotations

import logging
import re
import requests
from typing import List, Dict, Any, Optional

logger = logging.getLogger("worldcup_poller.sources.threesixtyfive")

# Base URL for 365Scores API
BASE_URL = "https://webws.365scores.com"

# Default headers (mimicking browser request)
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.365scores.com/",
    "Origin": "https://www.365scores.com",
}


def is_game_finished(game: Dict[str, Any]) -> bool:
    """
    Determine if a game has finished.

    365Scores uses "Ended" as the primary status text for completed games.
    Other possible values: "Finished", "FT", "Full Time", "AET", "Pen"

    Signals checked, in order:
      1. game.chartEvents.statuses[0].isFinished -- explicit bool
      2. game.justEnded -- fires the moment a match ends
      3. game.statusText -- confirmed 365Scores value is "Ended"
      4. game.gameTime >= 90 with no extra time
    """
    # Check 1: Explicit isFinished flag
    try:
        statuses = (game.get("chartEvents") or {}).get("statuses") or []
        if statuses and "isFinished" in statuses[0]:
            return bool(statuses[0]["isFinished"])
    except (AttributeError, IndexError, TypeError):
        pass

    # Check 2: justEnded flag
    if game.get("justEnded"):
        return True

    # Check 3: statusText - 365Scores uses "Ended"
    status_text = (game.get("statusText") or "").strip().lower()
    finished_keywords = [
        "ended",
        "finished",
        "ft",
        "full-time",
        "aet",
        "pen",
        "penalties",
    ]
    if status_text in finished_keywords:
        return True

    # Check 4: Time-based fallback - if gameTime >= 90 and not extra time
    time_elapsed = game.get("gameTime", 0)
    if time_elapsed >= 90:
        # Don't mark if it's half time or extra time
        if "half" not in status_text and "extra" not in status_text:
            # Also check if we have a winner (both scores set)
            home_comp = game.get("homeCompetitor", {})
            away_comp = game.get("awayCompetitor", {})
            if (
                home_comp.get("score") is not None
                and away_comp.get("score") is not None
            ):
                return True

    # Check 5: If game has ended but statusText contains "ended" in any form
    if "ended" in status_text:
        return True

    return False


def fetch_games_by_competition(
    competition_ids: List[int],
    timezone_name: str = "Africa/Nairobi",
    user_country_id: int = 413,
    show_odds: bool = True,
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch games for given competition IDs using the /web/games/fixtures/ endpoint.
    """
    params = {
        "appTypeId": 5,
        "langId": 1,
        "timezoneName": timezone_name,
        "userCountryId": user_country_id,
        "competitions": ",".join(str(cid) for cid in competition_ids),
        "showOdds": str(show_odds).lower(),
        "includeTopBettingOpportunity": "1",
        "topBookmaker": "14",
    }

    url = f"{BASE_URL}/web/games/fixtures/"

    try:
        logger.debug(f"Fetching from {url} with params {params}")
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()
        games = data.get("games", [])
        logger.info(
            f"fetch_games_by_competition({competition_ids}): {len(games)} games returned"
        )

        return games

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch games from 365Scores: {e}")
        return None
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        return None


def fetch_games_by_date_range(
    competition_ids: List[int],
    start_date: str,
    end_date: str,
    timezone_name: str = "Africa/Nairobi",
    user_country_id: int = 413,
    show_odds: bool = True,
) -> Optional[List[Dict[str, Any]]]:
    """
    Same endpoint as fetch_games_by_competition, but scoped to a date
    window via startDate/endDate. Needed for Club Friendlies
    (competitionId=321): that single competition pools every friendly
    for 6000+ clubs worldwide, so fetching it without a date bound
    would return a huge, mostly-irrelevant payload. startDate/endDate
    are accepted by the same /web/games/fixtures/ endpoint used in
    fetch_games_by_competition -- both params are "YYYY-MM-DD".
    """
    params = {
        "appTypeId": 5,
        "langId": 1,
        "timezoneName": timezone_name,
        "userCountryId": user_country_id,
        "competitions": ",".join(str(cid) for cid in competition_ids),
        "startDate": start_date,
        "endDate": end_date,
        "showOdds": str(show_odds).lower(),
        "includeTopBettingOpportunity": "1",
        "topBookmaker": "14",
    }

    url = f"{BASE_URL}/web/games/fixtures/"

    try:
        logger.debug(f"Fetching from {url} with params {params}")
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()
        games = data.get("games", [])
        logger.info(
            f"fetch_games_by_date_range({competition_ids}, {start_date}..{end_date}): "
            f"{len(games)} games returned"
        )

        return games

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch games from 365Scores: {e}")
        return None
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        return None


def filter_games_by_club_names(
    games: List[Dict[str, Any]],
    club_names: List[str],
) -> List[Dict[str, Any]]:
    """
    Keep only games where the home OR away competitor name contains
    (case-insensitive) one of club_names. Needed because 365Scores has
    no per-parent-league friendlies competitionId -- Club Friendlies
    (id 321) pools every club worldwide, so narrowing down to "just
    EPL clubs' friendlies" or "just Serie A clubs' friendlies" has to
    happen client-side against team names, not via a competitionId.

    Substring match (not exact-equals) because 365Scores' name field
    sometimes carries extra qualifiers 365Scores itself adds for
    disambiguation (e.g. suffixed age-group/reserve-team markers on
    otherwise-identical club names) -- config.py's *_CLUB_NAMES lists
    already carry the common aliases (e.g. "Man City", "Spurs") to
    catch 365Scores' own display-name variants.
    """
    needles = [n.lower() for n in club_names]

    def _matches(name: Optional[str]) -> bool:
        if not name:
            return False
        name_lower = name.lower()
        return any(needle in name_lower for needle in needles)

    filtered = []
    for game in games:
        home_name = (game.get("homeCompetitor") or {}).get("name")
        away_name = (game.get("awayCompetitor") or {}).get("name")
        if _matches(home_name) or _matches(away_name):
            filtered.append(game)

    return filtered


def fetch_game_details(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int,
    lang_id: int = 1,
    user_country_id: int = 413,
) -> Optional[Dict[str, Any]]:
    """
    Fetch full game details including lineups using the /web/game/ endpoint.

    Args:
        game_id: 365Scores game ID (e.g., "4627864")
        away_id: Away team competitor ID
        home_id: Home team competitor ID
        competition_id: Competition ID (e.g., 5930)
        lang_id: Language ID (1 = English)
        user_country_id: Country ID (413 = Kenya)

    Returns:
        Full game data including lineups, statistics, events, commentary
    """
    matchup_id = f"{away_id}-{home_id}-{competition_id}"

    params = {
        "appTypeId": 5,
        "langId": lang_id,
        "timezoneName": "Africa/Nairobi",
        "userCountryId": user_country_id,
        "gameId": game_id,
        "matchupId": matchup_id,
    }

    url = f"{BASE_URL}/web/game/"

    try:
        logger.debug(f"Fetching game details from {url} with params {params}")
        response = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()
        logger.info(f"fetch_game_details({game_id}): Success")
        return data

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch game details for {game_id}: {e}")
        return None
    except ValueError as e:
        logger.error(f"Failed to parse JSON response for {game_id}: {e}")
        return None


def fetch_lineups(
    game_id: str, away_id: int, home_id: int, competition_id: int
) -> Optional[Dict[str, Any]]:
    """
    Fetch only lineups from the game details endpoint.

    Returns:
        {
            "home": {
                "formation": "4-3-3",
                "status": "Confirmed",
                "members": [...]
            },
            "away": {
                "formation": "4-2-3-1",
                "status": "Confirmed",
                "members": [...]
            }
        }
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)

    if not data or "game" not in data:
        logger.warning(f"No game data found for {game_id}")
        return None

    game = data.get("game", {})

    home_competitor = game.get("homeCompetitor", {})
    away_competitor = game.get("awayCompetitor", {})

    home_lineups = home_competitor.get("lineups")
    away_lineups = away_competitor.get("lineups")

    if not home_lineups and not away_lineups:
        logger.debug(f"No lineups available for {game_id}")
        return None

    # Player names live in a separate top-level "members" array on the
    # game object, keyed by the same "id" used inside lineups.members[].
    # The lineup entries themselves never include a name field, so we
    # have to join them here.
    roster = {m["id"]: m for m in game.get("members", []) if "id" in m}

    def _attach_names(lineup: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not lineup:
            return {}
        for player in lineup.get("members", []):
            info = roster.get(player.get("id"))
            if info:
                player["name"] = info.get("name")
                player["shortName"] = info.get("shortName")
                player["athleteId"] = info.get("athleteId")
        return lineup

    home_lineups = _attach_names(home_lineups)
    away_lineups = _attach_names(away_lineups)

    result = {
        "fixture_id": f"wc26_{game_id}",
        "home": home_lineups or {},
        "away": away_lineups or {},
    }

    logger.info(f"fetch_lineups({game_id}): Found lineups")
    return result


# All keywords are matched with regex word boundaries (\b), never plain
# substring containment -- naive "in" checks cause false positives like
# "pen" matching inside "suspended", or "ended" matching inside
# "suspended" too. \b works fine for multi-word phrases like "half time"
# since spaces are already non-word characters.
HALFTIME_STATUS_KEYWORDS = ("ht", "half time", "halftime")
STOPPED_STATUS_KEYWORDS = (
    "stopped",
    "suspended",
    "interrupted",
    "delayed",
    "abandoned",
)
FULLTIME_STATUS_KEYWORDS = (
    "ft",
    "aet",
    "pen",
    "ended",
    "finished",
    "full-time",
    "full time",
    "penalties",
)


def _matches(text: str, keywords: tuple) -> bool:
    return any(re.search(r"\b" + re.escape(kw) + r"\b", text) for kw in keywords)


def classify_match_phase(status_text: Optional[str]) -> Optional[str]:
    """
    Classify a 365Scores statusText into one of the three moments we care
    about for statistics snapshots: "halftime", "stopped", "fulltime".
    Returns None if the match is in open play (or status is unknown).
    """
    text = (status_text or "").strip().lower()
    if not text:
        return None
    if _matches(text, FULLTIME_STATUS_KEYWORDS):
        return "fulltime"
    if _matches(text, HALFTIME_STATUS_KEYWORDS):
        return "halftime"
    if _matches(text, STOPPED_STATUS_KEYWORDS):
        return "stopped"
    return None


def extract_statistics_from_game(game: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the statistics payload from an already-fetched `game` object.
    Lets callers that already hold a `game` dict (e.g. the poller's live
    loop) skip a redundant fetch_game_details() call.
    """
    return {
        "home": {
            "possession": game.get("homePossession"),
            "shots": game.get("homeShots"),
            "shots_on_target": game.get("homeShotsOnTarget"),
            "shots_off_target": game.get("homeShotsOffTarget"),
            "corners": game.get("homeCorners"),
            "fouls": game.get("homeFouls"),
            "yellow_cards": game.get("homeYellowCards"),
            "red_cards": game.get("homeRedCards"),
            "offsides": game.get("homeOffsides"),
            "passes": game.get("homePasses"),
            "pass_accuracy": game.get("homePassAccuracy"),
        },
        "away": {
            "possession": game.get("awayPossession"),
            "shots": game.get("awayShots"),
            "shots_on_target": game.get("awayShotsOnTarget"),
            "shots_off_target": game.get("awayShotsOffTarget"),
            "corners": game.get("awayCorners"),
            "fouls": game.get("awayFouls"),
            "yellow_cards": game.get("awayYellowCards"),
            "red_cards": game.get("awayRedCards"),
            "offsides": game.get("awayOffsides"),
            "passes": game.get("awayPasses"),
            "pass_accuracy": game.get("awayPassAccuracy"),
        },
        "minute": int(game.get("gameTime", 0) or 0),
        "status_text": game.get("statusText"),
    }


def fetch_statistics(
    game_id: str, away_id: int, home_id: int, competition_id: int
) -> Optional[Dict[str, Any]]:
    """
    Fetch statistics from the game details endpoint.

    NOTE: this makes its own fetch_game_details() call. If you already
    have a `game` object on hand, prefer extract_statistics_from_game(game)
    to avoid a redundant network request.
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)

    if not data or "game" not in data:
        return None

    game = data.get("game", {})
    return extract_statistics_from_game(game)


# ============================================================================
# PLAY-BY-PLAY FEED (shared by fetch_commentary and fetch_match_events)
# ----------------------------------------------------------------------------
# The /web/game/ endpoint does NOT embed commentary or a discrete events
# list directly -- game.commentary and game.events are not real fields.
# It only returns game.playByPlay.feedURL, a pointer to a separate feed
# (pbpgenerator.365scores.com). That feed is the ONLY place 365Scores
# exposes per-event type/minute/side data; the cumulative counters used
# by extract_statistics_from_game() (homeCorners, homeYellowCards, ...)
# have no per-event breakdown.
#
# THIS SHARED HELPER WAS MISSING from the league version of this file --
# fetch_commentary() and fetch_match_events() each duplicated their own
# raw fetch instead of sharing one. Restored to match the World Cup
# version so both functions stay consistent and only need one network
# call's worth of logic to maintain.
#
# Two things the raw feed gets wrong that we correct here:
#   1. The feedURL 365Scores returns comes pre-built with lang=37
#      (Dutch), not English -- every other endpoint in this file uses
#      langId=1, so we force lang=1 here too before fetching.
#   2. Entry field names are PascalCase (.NET-style): Comment, Timeline,
#      Type, TypeName, Period, Title, IsMajor, Players, CompetitorNum --
#      not the lowercase minute/text/type/team/player shape used
#      elsewhere in this codebase.
# ============================================================================


def _fetch_play_by_play_raw(
    game_id: str, away_id: int, home_id: int, competition_id: int
) -> List[Dict[str, Any]]:
    """Shared fetch + unwrap for the play-by-play feed. Both
    fetch_commentary() and fetch_match_events() parse this same raw list
    into different shapes -- this only does the network call + finding
    the entry list in the response, not any field-level interpretation.
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)

    if not data or "game" not in data:
        return []

    game = data.get("game", {})
    pbp = game.get("playByPlay") or {}
    feed_url = pbp.get("feedURL")

    if not feed_url:
        logger.debug(f"No playByPlay feedURL for {game_id}")
        return []

    # Force English -- 365Scores returns lang=37 (Dutch) by default here.
    feed_url = re.sub(r"lang=\d+", "lang=1", feed_url)

    try:
        response = requests.get(feed_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        raw = response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch play-by-play feed for {game_id}: {e}")
        return []
    except ValueError as e:
        logger.error(f"Failed to parse play-by-play JSON for {game_id}: {e}")
        return []

    # We don't rely on knowing the exact wrapper key name -- find the
    # first top-level list of dicts in the response instead.
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value

    return []


def _competitor_num_to_side(competitor_num: Any) -> Optional[str]:
    """365Scores' play-by-play feed marks each entry with CompetitorNum
    (1 or 2), not a competitor id -- it can't be joined against
    homeCompetitor/awayCompetitor.id the way fetch_lineups() joins
    roster members.

    UNCONFIRMED against a live payload: assuming NUM 1 = home,
    NUM 2 = away, matching the ordering 365Scores uses everywhere else
    in this file (homeCompetitor first, awayCompetitor second). Verify
    against one real play-by-play response before trusting this for
    actual sub-fixture settlement -- if it's backwards, every
    first_goal/first_card/first_corner market will settle to the wrong
    team.
    """
    if competitor_num == 1:
        return "home"
    if competitor_num == 2:
        return "away"
    return None


def fetch_commentary(
    game_id: str, away_id: int, home_id: int, competition_id: int
) -> List[Dict[str, Any]]:
    """
    Fetch commentary via 365Scores' separate play-by-play feed. See the
    module-level comment above _fetch_play_by_play_raw for why this
    can't come from the /web/game/ response directly.

    Returns:
        List of commentary entries with:
        {
            "minute": int,
            "text": str,
            "type": str,
            "team": Optional[str],   # "home" | "away" | None
            "player": Optional[str],
        }
        Note: createdAt is added by the poller when forwarding.
    """
    raw_commentary = _fetch_play_by_play_raw(game_id, away_id, home_id, competition_id)
    if not raw_commentary:
        return []

    commentary_list = []
    for entry in raw_commentary:
        minute_raw = entry.get("Timeline")
        try:
            minute = int(minute_raw) if minute_raw is not None else 0
        except (ValueError, TypeError):
            minute = 0

        text = entry.get("Comment") or entry.get("Title") or ""

        players = entry.get("Players") or []
        player = players[0].get("PlayerName") if players else None

        commentary_list.append(
            {
                "minute": minute,
                "text": text,
                "type": entry.get("TypeName", "commentary"),
                # FIX: this was hardcoded to None in the league version --
                # restored to actually resolve the side via CompetitorNum,
                # same as fetch_match_events() already did correctly.
                "team": _competitor_num_to_side(entry.get("CompetitorNum")),
                "player": player,
            }
        )

    logger.info(f"fetch_commentary({game_id}): Found {len(commentary_list)} entries")
    return commentary_list


# TypeName markers for classifying play-by-play entries into the three
# sub-fixture event buckets. Matched as substrings against the lowercased
# TypeName -- these are guesses at 365Scores' actual English TypeName
# strings ("Goal", "Yellow Card", "Red Card", "Corner", etc.) based on
# common convention across similar feeds; confirm against a real payload
# and adjust if 365Scores uses different wording.
_CARD_TYPENAME_MARKERS = ("yellow card", "red card", "second yellow")
_CORNER_TYPENAME_MARKERS = ("corner",)
_GOAL_TYPENAME_MARKERS = (
    "goal",
)  # checked last: "goal" is a substring of nothing above, but keep order defensive


def _classify_event_typename(type_name: Optional[str]) -> Optional[str]:
    t = (type_name or "").strip().lower()
    if any(m in t for m in _CARD_TYPENAME_MARKERS):
        return "card"
    if any(m in t for m in _CORNER_TYPENAME_MARKERS):
        return "corner"
    if any(m in t for m in _GOAL_TYPENAME_MARKERS):
        return "goal"
    return None


def fetch_match_events(
    game_id: str, away_id: int, home_id: int, competition_id: int
) -> List[Dict[str, Any]]:
    """
    Discrete goal/card/corner events, derived from the SAME play-by-play
    feed fetch_commentary() reads. This is what feeds the first_goal /
    first_card / first_corner sub-fixture markets -- there is no other
    per-event data source in this API client.

    Returns [{event_type, minute, team, player}, ...] for entries
    classified as goal/card/corner, sorted by minute. Anything else in
    the feed (kickoff markers, half-end markers, general commentary) is
    skipped here -- fetch_commentary() still returns those separately
    for the chat/commentary feed; this makes its own network call rather
    than sharing a single fetch with fetch_commentary(), consistent with
    how fetch_statistics()/fetch_lineups()/fetch_commentary() each
    already make independent fetch_game_details() calls in this file.

    KNOWN GAPS, both flagged inline where they matter:
      - Team attribution (_competitor_num_to_side) assumes
        CompetitorNum 1=home, 2=away -- unconfirmed against a live
        payload.
      - Own goals are not special-cased. A TypeName containing "goal"
        for an own goal will attribute the event to whichever side
        CompetitorNum points at (likely the scoring player's own team),
        which is backwards for a first_goal market -- an own goal by
        the away team should count as a home team's "first goal" for
        settlement purposes. Needs a real payload to know whether
        365Scores' TypeName distinguishes "Own Goal" from "Goal" so
        this can be corrected.
    """
    raw_commentary = _fetch_play_by_play_raw(game_id, away_id, home_id, competition_id)
    if not raw_commentary:
        return []

    events: List[Dict[str, Any]] = []
    for entry in raw_commentary:
        event_type = _classify_event_typename(entry.get("TypeName"))
        if event_type is None:
            continue

        team = _competitor_num_to_side(entry.get("CompetitorNum"))
        if team is None:
            logger.debug(
                f"{game_id}: skipping {event_type} event with unresolvable team "
                f"(CompetitorNum={entry.get('CompetitorNum')!r})"
            )
            continue

        minute_raw = entry.get("Timeline")
        try:
            minute = int(minute_raw) if minute_raw is not None else 0
        except (ValueError, TypeError):
            minute = 0

        players = entry.get("Players") or []
        player = players[0].get("PlayerName") if players else None

        events.append(
            {
                "event_type": event_type,
                "minute": minute,
                "team": team,
                "player": player,
            }
        )

    events.sort(key=lambda e: e["minute"])
    logger.info(
        f"fetch_match_events({game_id}): Found {len(events)} goal/card/corner events"
    )
    return events


def fetch_complete_match_data(
    game_id: str, away_id: int, home_id: int, competition_id: int
) -> Optional[Dict[str, Any]]:
    """
    Fetch all match data: details, lineups, statistics, and commentary in one go.
    """
    data = fetch_game_details(game_id, away_id, home_id, competition_id)

    if not data or "game" not in data:
        return None

    game = data.get("game", {})

    return {
        "game_id": game_id,
        "details": game,
        "lineups": {
            "home": game.get("homeCompetitor", {}).get("lineups", {}),
            "away": game.get("awayCompetitor", {}).get("lineups", {}),
        },
        "statistics": {
            "home": {
                "possession": game.get("homePossession"),
                "shots": game.get("homeShots"),
                "shots_on_target": game.get("homeShotsOnTarget"),
                "shots_off_target": game.get("homeShotsOffTarget"),
                "corners": game.get("homeCorners"),
                "fouls": game.get("homeFouls"),
                "yellow_cards": game.get("homeYellowCards"),
                "red_cards": game.get("homeRedCards"),
                "offsides": game.get("homeOffsides"),
                "passes": game.get("homePasses"),
                "pass_accuracy": game.get("homePassAccuracy"),
            },
            "away": {
                "possession": game.get("awayPossession"),
                "shots": game.get("awayShots"),
                "shots_on_target": game.get("awayShotsOnTarget"),
                "shots_off_target": game.get("awayShotsOffTarget"),
                "corners": game.get("awayCorners"),
                "fouls": game.get("awayFouls"),
                "yellow_cards": game.get("awayYellowCards"),
                "red_cards": game.get("awayRedCards"),
                "offsides": game.get("awayOffsides"),
                "passes": game.get("awayPasses"),
                "pass_accuracy": game.get("awayPassAccuracy"),
            },
            "minute": int(game.get("gameTime", 0) or 0),
        },
        "commentary": game.get("commentary", []),
        "score": {
            "home": game.get("homeCompetitor", {}).get("score", 0),
            "away": game.get("awayCompetitor", {}).get("score", 0),
        },
        "status": game.get("statusText"),
        "time_elapsed": int(game.get("gameTime", 0) or 0),
        "is_finished": is_game_finished(game),
    }
