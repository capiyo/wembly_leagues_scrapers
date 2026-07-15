"""
MongoDB access for the poller. Field names match the Rust Game struct
EXACTLY (camelCase, per each #[serde(rename = "...")]) -- this was
previously broken: the file's docstring claimed to match Rust but every
write/query used snake_case, causing every fixture document to fail
deserialization on the Rust side ("invalid type: map, expected a string" /
documents silently skipped in GET /api/games).

Handles: fixtures, lineups, statistics, events, commentary, state management.

NOTE: Flashscore cross-reference bookkeeping (flashscore_id,
flashscore_resolve_attempts, needs_flashscore_resolution(), etc.) has been
removed -- commentary now comes from 365Scores via sources/threesixtyfive.py,
so there's no separate ID resolution step needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, List, Dict

from pymongo import MongoClient
from pymongo.collection import Collection

import config

logger = logging.getLogger("worldcup_poller.mongo")


class FixtureStore:
    def __init__(self, mongo_uri: str):
        self._client = MongoClient(mongo_uri)
        self._collection: Collection = self._client[config.MONGO_DB][config.MONGO_COLLECTION]
        # Separate small collection for scraper-wide state (currently just
        # the rolling reference date used to anchor the priority-league
        # scrape window). Kept apart from `games` since it's a single
        # config-like document, not a fixture.
        self._state: Collection = self._client[config.MONGO_DB]["scraper_state"]
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create indexes for fast queries. Index keys use the same
        camelCase field names actually stored on documents."""
        try:
            self._collection.create_index("matchId", unique=True)
            self._collection.create_index("threesixtyfiveGameId")
            self._collection.create_index("status")
            self._collection.create_index([("status", 1), ("isLive", 1)])
            self._collection.create_index("kickoffUtc")
            self._collection.create_index("scrapedAt")
            self._collection.create_index("forwardedEventSignatures")
            self._collection.create_index([("leagueKey", 1), ("roundNum", 1)])
            logger.info("MongoDB indexes ensured")
        except Exception as e:
            logger.warning(f"Index creation issue: {e}")

    # ============================================================
    # FIXTURE CRUD OPERATIONS
    # ============================================================

    def upsert_fixture(
        self,
        match_id: str,
        threesixtyfive_game_id: str,
        home_team: str,
        away_team: str,
        kickoff_utc: datetime,
        status: str,
        home_competitor_id: Optional[str] = None,
        away_competitor_id: Optional[str] = None,
        competition_id: Optional[int] = None,
        competition_name: str = "FIFA World Cup 2026",
        odds: dict = None,
        league_key: Optional[str] = None,
        round_num: Optional[int] = None,
        round_name: Optional[str] = None,
        group_num: Optional[int] = None,
        group_name: Optional[str] = None,
    ) -> None:
        """
        Upsert a fixture. Document keys match Rust's Game struct exactly
        (see models/game.rs): matchId, homeTeam, awayTeam, kickoffUtc,
        isLive, availableForVoting, homeWin/awayWin, scrapedAt, etc.

        NOTE: status / isLive / availableForVoting are ONLY written on
        INSERT (via $setOnInsert below), never on update. poller.py's
        MatchStateMachine is the sole owner of those three fields for the
        lifetime of a fixture -- scraper.py re-runs periodically just to
        pick up new fixtures, and previously re-upserting an EXISTING
        fixture would stomp poller.py's correct "soon"/"live" state back
        to whatever scraper.py's own (cruder, statusText-based) guess was,
        causing fixtures to flip back to "live" while still an hour from
        kickoff. `status` is still accepted as a param here because it's
        needed for the initial insert.
        """
        date_str = kickoff_utc.strftime("%Y-%m-%d")
        time_str = kickoff_utc.strftime("%H:%M")
        date_iso = kickoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Default odds to 1.0 if not provided
        home_win = 1.0
        away_win = 1.0
        draw = 1.0
        if odds:
            home_win = odds.get("homeWin", 1.0)
            away_win = odds.get("awayWin", 1.0)
            draw = odds.get("draw", 1.0)

        # Build the document -- camelCase keys matching Game's #[serde(rename)]
        # status/isLive/availableForVoting deliberately NOT here anymore --
        # moved to set_on_insert below.
        doc = {
            "matchId": match_id,
            "threesixtyfiveGameId": threesixtyfive_game_id,
            "homeTeam": home_team,
            "awayTeam": away_team,
            # Bookkeeping fields, not on the Rust struct -- harmless extras,
            # ignored by serde on read. Kept snake_case to make clear
            # they're Python-side only, not part of the Rust contract.
            "home_competitor_id": home_competitor_id,
            "away_competitor_id": away_competitor_id,
            "competition_id": competition_id,
            "league": competition_name,
            # New for the multi-league scraper (leagues_scraper.py). None
            # for World Cup docs written by the original scraper.py.
            "leagueKey": league_key,
            "roundNum": round_num,
            "roundName": round_name,
            "groupNum": group_num,
            "groupName": group_name,
            "date": date_str,
            "time": time_str,
            "dateIso": date_iso,
            # kickoffUtc is chrono::DateTime<Utc> on the Rust side (every
            # OTHER timestamp field on Game is mongodb::bson::DateTime,
            # which deserializes fine from a native BSON Date -- this one
            # is the sole exception). chrono's serde Deserialize impl
            # expects an RFC3339 string, not a raw BSON Date document, so
            # this must be passed as a string, not a Python datetime
            # object (which pymongo would otherwise encode as a native
            # BSON Date and fail deserialization on the Rust side).
            "kickoffUtc": kickoff_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "homeWin": home_win,
            "awayWin": away_win,
            "draw": draw,
            "scrapedAt": datetime.now(timezone.utc),
            "source": "365scores",
            "lastScrapedAt": datetime.now(timezone.utc),
        }

        # Fields that should ONLY be set on insert (user-generated data
        # preserved, and now also status/isLive/availableForVoting/scores --
        # once a fixture exists, only poller.py's state machine and
        # update_score()/update_status() are allowed to change these).
        is_live = status == "live"
        available_for_voting = status in ("upcoming", "soon")

        set_on_insert = {
            # CRITICAL: explicitly set _id to the same string as matchId.
            # Without this, MongoDB auto-generates _id as a BSON ObjectId.
            # Rust's Game.id field is `Option<String>` (#[serde(rename =
            # "_id")]) -- an ObjectId does NOT deserialize into a plain
            # String via serde (it needs bson::oid::ObjectId specifically,
            # or a string representation). This single mismatched field
            # was the actual cause of EVERY "invalid type: map, expected a
            # string" / "skipping malformed fixture document" error, even
            # after every other field was correctly renamed to camelCase --
            # the camelCase fix was necessary but not sufficient.
            "_id": match_id,
            "status": status,
            "isLive": is_live,
            "availableForVoting": available_for_voting,
            "homeScore": None,
            "awayScore": None,
            "votes": 0,
            "voters": [],
            "comments": 0,
            "commentary": [],
            "commentaryCount": 0,
            "lastCommentaryAt": None,
            "lineups": None,
            "lineupsFetched": False,
            "lineupsFetchedAt": None,
            "statistics": [],
            "lastStatisticsMinute": None,
            "forwardedEventSignatures": [],
            "lastPolledAt": None,
            "completedAt": None,
            "movedToHistory": False,
            "createdAt": datetime.now(timezone.utc),
            "result": None,
            "timeElapsed": None,
        }

        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": doc,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

    def get_fixture(self, match_id: str) -> Optional[Dict[str, Any]]:
        """Get a single fixture by match_id."""
        return self._collection.find_one({"matchId": match_id})

    def get_fixtures_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get all fixtures with a given status."""
        return list(self._collection.find({"status": status}))

    def get_all_fixtures(self) -> List[Dict[str, Any]]:
        """Get all fixtures (all statuses)."""
        return list(self._collection.find({}))

    def get_fixtures_in_window(self, days_ahead: int = 7) -> List[Dict[str, Any]]:
        """Get fixtures within the next N days."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days_ahead)
        return list(self._collection.find({
            "kickoffUtc": {"$gte": now, "$lte": cutoff}
        }))

    def get_fixtures_by_league(self, league_key: str) -> List[Dict[str, Any]]:
        """Get all games for a given league (e.g. 'epl', 'ucl', 'facup')."""
        return list(self._collection.find({"leagueKey": league_key}))

    def get_latest_kickoff_for_league(self, league_key: str) -> Optional[datetime]:
        """Return the kickoff time of the furthest-out fixture already
        stored for this league -- i.e. the current 'high-water mark' of
        what leagues_scraper.py has already scraped. Returns None if
        nothing has been scraped for this league yet.

        Used as the rolling-window cursor: instead of every scrape
        re-anchoring on 'today' (which does nothing useful while a
        league's season is still weeks away), the next window picks up
        from where the previous one left off.

        kickoffUtc is stored as an ISO-8601 UTC string (see
        upsert_fixture's comment on why it can't be a native BSON Date),
        so sorting on it lexicographically matches chronological order.
        """
        doc = self._collection.find_one(
            {"leagueKey": league_key},
            sort=[("kickoffUtc", -1)],
            projection={"kickoffUtc": 1},
        )
        if not doc or not doc.get("kickoffUtc"):
            return None
        try:
            return datetime.fromisoformat(doc["kickoffUtc"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    # ============================================================
    # REFERENCE-DATE STATE (rolling window anchor for priority leagues)
    # ============================================================
    #
    # Instead of anchoring the scrape window on `datetime.now()` -- which
    # right now sits in a dead zone before any of the priority leagues
    # (EPL/UCL/Europa/FA Cup/Community Shield) have fixtures -- we anchor
    # on a single stored "reference date" that starts at
    # config.REFERENCE_DATE_DEFAULT and creeps forward by exactly one day
    # per real calendar day, regardless of how many times the poller
    # triggers a rescrape on a given day (0, 1, or a burst of several
    # after multiple matches complete).
    #
    # This is enforced atomically via find_one_and_update with the
    # "lastIncrementedDate != today" condition baked into the filter, so
    # concurrent/repeated calls on the same day are a no-op after the
    # first one -- no separate boolean flag to remember to reset.

    _REFERENCE_STATE_ID = "reference_window"

    def get_reference_date(self) -> datetime:
        """Return the current reference date (UTC midnight). Seeds the
        state document with config.REFERENCE_DATE_DEFAULT on first call
        if it doesn't exist yet."""
        doc = self._state.find_one({"_id": self._REFERENCE_STATE_ID})
        if not doc:
            default = datetime.fromisoformat(config.REFERENCE_DATE_DEFAULT).replace(tzinfo=timezone.utc)
            self._state.update_one(
                {"_id": self._REFERENCE_STATE_ID},
                {
                    "$setOnInsert": {
                        "referenceDate": default.isoformat(),
                        "lastIncrementedDate": None,
                    }
                },
                upsert=True,
            )
            return default
        return datetime.fromisoformat(doc["referenceDate"].replace("Z", "+00:00"))

    def advance_reference_date_if_needed(self) -> datetime:
        """Return the effective reference date: max(today, seed).

        Previously this incremented a persisted value by exactly +1 day
        per real calendar day, starting from config.REFERENCE_DATE_DEFAULT.
        That seemed equivalent to "track real time" but wasn't: the GAP
        between referenceDate and the real clock was fixed at whatever it
        happened to be the day this table was first seeded, and never
        closed -- because both values were advancing at the same +1/day
        rate independently, in parallel, rather than one tracking the
        other. If the poller had already been running for N days before
        the seed date arrived, referenceDate stayed permanently ~N days
        ahead of "today", racing straight through real near-term fixtures
        (e.g. the Community Shield) while they were still weeks away in
        real time, and never coming back around to include them since the
        value only ever climbed.

        max(now, seed) has no such drift: before "today" reaches the seed
        date it holds steady at the seed (skipping the pre-season dead
        zone, same as before), and the moment real "today" reaches or
        passes the seed date, the reference simply *is* today from then
        on -- permanently in sync, no persisted increment needed.

        Still writes the state doc on every call (cheap upsert) purely
        for observability/debugging -- nothing downstream depends on the
        stored value being anything other than a mirror of the return
        value here.
        """
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        seed = datetime.fromisoformat(config.REFERENCE_DATE_DEFAULT).replace(tzinfo=timezone.utc)
        reference = max(today, seed)

        self._state.update_one(
            {"_id": self._REFERENCE_STATE_ID},
            {
                "$set": {
                    "referenceDate": reference.isoformat(),
                    "lastComputedAt": datetime.now(timezone.utc).isoformat(),
                },
            },
            upsert=True,
        )

        logger.info("Reference date = %s (max(today, seed))", reference.strftime("%Y-%m-%d"))
        return reference

    def get_fixtures_by_league_round(self, league_key: str, round_num: int) -> List[Dict[str, Any]]:
        """Get all games for a given league + round number."""
        return list(self._collection.find({"leagueKey": league_key, "roundNum": round_num}))

    def get_active_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures that are upcoming, soon, or live."""
        return list(self._collection.find({
            "status": {"$in": ["upcoming", "soon", "live"]}
        }))

    def get_in_progress_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures that are currently live."""
        return list(self._collection.find({"status": "live"}))

    def get_upcoming_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures that are upcoming or soon."""
        return list(self._collection.find({
            "status": {"$in": ["upcoming", "soon"]}
        }))

    def get_soon_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures in the 'soon' state."""
        return list(self._collection.find({"status": "soon"}))

    def get_completed_fixtures(self) -> List[Dict[str, Any]]:
        """Get fixtures that are completed."""
        return list(self._collection.find({"status": "completed"}))

    def get_stale_completed_fixtures(self, hours: int = 1) -> List[Dict[str, Any]]:
        """Get completed fixtures older than N hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return list(self._collection.find({
            "status": "completed",
            "completedAt": {"$lt": cutoff}
        }))

    def get_threesixtyfive_game_id(self, match_id: str) -> Optional[str]:
        """Get the 365Scores game ID for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"threesixtyfiveGameId": 1}
        )
        return doc.get("threesixtyfiveGameId") if doc else None

    def get_game(self, match_id: str) -> Optional[Dict[str, Any]]:
        """Get full game document (alias for get_fixture)."""
        return self.get_fixture(match_id)

    # ============================================================
    # STATUS UPDATES
    # ============================================================

    def update_status(self, match_id: str, status: str) -> None:
        """Update match status."""
        is_live = status == "live"
        available_for_voting = status in ("upcoming", "soon")

        update = {
            "status": status,
            "isLive": is_live,
            "availableForVoting": available_for_voting,
            "scrapedAt": datetime.now(timezone.utc),
        }

        if status == "completed":
            update["completedAt"] = datetime.now(timezone.utc)

        self._collection.update_one(
            {"matchId": match_id},
            {"$set": update}
        )

    def update_score(self, match_id: str, home_score: int, away_score: int) -> None:
        """Update score for a match."""
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": {
                    "homeScore": home_score,
                    "awayScore": away_score,
                    "scrapedAt": datetime.now(timezone.utc),
                }
            }
        )

    def update_time_elapsed(self, match_id: str, time_elapsed: int) -> None:
        """Update the elapsed time for a match."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$set": {"timeElapsed": time_elapsed}}
        )

    def mark_live(self, match_id: str) -> None:
        """Mark a match as live."""
        self.update_status(match_id, "live")

    def mark_completed(self, match_id: str) -> None:
        """Mark a match as completed."""
        self.update_status(match_id, "completed")

    def record_last_poll(self, match_id: str) -> None:
        """Record last poll time. NOTE: lastPolledAt is not on the Rust
        Game struct shown -- harmless extra field, ignored by serde."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$set": {"lastPolledAt": datetime.now(timezone.utc)}}
        )

    # ============================================================
    # LINEUPS
    # ============================================================
    # NOTE: Rust's Game.lineups is Option<LineupsDocument>, a TYPED
    # struct (homeLineup/awayLineup, each with formation/coach/players/
    # bench), not an arbitrary dict. If `lineups` here doesn't match that
    # exact shape, Rust will fail to deserialize the whole Game document
    # once this field is populated -- same class of bug as the field-name
    # mismatch that caused the original "skipping malformed fixture" errors.
    # The actual lineups write path in this codebase goes through the Rust
    # API's own /games/lineups handler (store_lineups in games.rs), which
    # builds the LineupsDocument shape correctly on the Rust side -- these
    # Python-side methods are kept for local/back-compat use but should NOT
    # be the primary write path while that Rust endpoint exists.

    def store_lineups(self, match_id: str, lineups: Dict) -> None:
        """Store lineups and mark as fetched.

        CAUTION: see note above -- prefer forwarding to the Rust
        /games/lineups endpoint (already done via forwarder.py) over
        writing this field directly from Python, to avoid shape drift."""
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": {
                    "lineups": lineups,
                    "lineupsFetched": True,
                    "lineupsFetchedAt": datetime.now(timezone.utc),
                    "scrapedAt": datetime.now(timezone.utc),
                }
            },
            upsert=False,  # NOTE: was upsert=True -- creating a fixture doc from
            # a lineups write alone produced partial/zombie documents once the
            # real fixture no longer existed (already archived to history).
            # Lineups should never be the thing that creates a fixture.
        )

    def mark_lineups_fetched(self, match_id: str) -> None:
        """Mark that lineups have been fetched."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$set": {"lineupsFetched": True, "lineupsFetchedAt": datetime.now(timezone.utc)}}
        )

    def get_lineups(self, match_id: str) -> Optional[Dict]:
        """Get stored lineups for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"lineups": 1, "lineupsFetched": 1}
        )
        return doc.get("lineups") if doc else None

    def lineups_available(self, match_id: str) -> bool:
        """Check if lineups are available for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"lineupsFetched": 1}
        )
        return doc.get("lineupsFetched", False) if doc else False

    # ============================================================
    # STATISTICS
    # ============================================================
    # NOTE: Rust's Game.statistics is Vec<StatisticsSnapshot>, each with a
    # TYPED `statistics: MatchStatistics { home: TeamStatistics, away:
    # TeamStatistics }` shape -- not an arbitrary dict. As with lineups,
    # the Rust API's own /games/statistics handlers (add_statistics_snapshot
    # / bulk_update_statistics in games.rs) build this shape correctly.
    # These Python methods write a generic `stats` dict directly and will
    # cause the same deserialization failure if that dict doesn't match
    # MatchStatistics's exact field names (possession, shots,
    # shotsOnTarget, etc. -- check TeamStatisticsPayload's snake_case
    # Deserialize impl specifically, since unlike Game/CommentaryEntry,
    # TeamStatisticsPayload has NO #[serde(rename)] attributes, meaning it
    # expects snake_case wire keys, not camelCase -- confirm against your
    # actual struct before relying on this path).

    def add_statistics_snapshot(self, match_id: str, stats: Dict, minute: int) -> None:
        """Add a statistics snapshot at a specific minute.

        CAUTION: see note above -- prefer forwarding to the Rust
        /games/statistics endpoint over writing this field directly."""
        # Defensive int() cast at the actual write boundary. Rust's
        # StatisticsSnapshot.minute is a strict i32 -- a float here (e.g.
        # 365Scores' gameTime returning 45.0 at halftime) gets stored as
        # a BSON double and crashes every subsequent /games/live and
        # /games/upcoming deserialization for this fixture with
        # "invalid type: floating point, expected i32" (see wc26_4749268
        # incident, 2026-07-03). Cast here too, not just at the caller,
        # so this method is safe no matter what calls it in the future.
        minute = int(minute or 0)
        snapshot = {
            "minute": minute,
            "statistics": stats,
            "timestamp": datetime.now(timezone.utc)
        }
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$push": {"statistics": snapshot},
                "$set": {
                    "lastStatisticsMinute": minute,
                    "scrapedAt": datetime.now(timezone.utc),
                }
            },
            upsert=False,  # NOTE: was upsert=True -- same zombie-doc risk as
            # add_commentary below. A statistics push should never be able to
            # create a fixture document out of thin air.
        )

    def get_statistics(self, match_id: str) -> List[Dict]:
        """Get all statistics snapshots for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"statistics": 1}
        )
        return doc.get("statistics", []) if doc else []

    def get_latest_statistics(self, match_id: str) -> Optional[Dict]:
        """Get the latest statistics snapshot for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"statistics": 1}
        )
        if doc and doc.get("statistics"):
            return doc["statistics"][-1]
        return None

    # ============================================================
    # EVENTS
    # ============================================================

    def get_forwarded_event_signatures(self, match_id: str) -> set:
        """Get the set of event signatures already forwarded."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"forwardedEventSignatures": 1}
        )
        if not doc:
            return set()
        return set(doc.get("forwardedEventSignatures", []))

    def add_forwarded_event_signature(self, match_id: str, signature: str) -> None:
        """Add a forwarded event signature."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$addToSet": {"forwardedEventSignatures": signature}},
            upsert=False,
        )

    def add_forwarded_event_signatures_bulk(self, match_id: str, signatures: List[str]) -> None:
        """Add multiple forwarded event signatures."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$addToSet": {"forwardedEventSignatures": {"$each": signatures}}},
            upsert=False,
        )

    # ============================================================
    # COMMENTARY
    # ============================================================
    # NOTE: Rust's CommentaryEntry struct requires minute: i32, type:
    # String (renamed from event_type), createdAt: BsonDateTime -- all
    # REQUIRED, no Option. The `entry` dict passed in here must already
    # contain "minute", "type", "createdAt" (or this write will cause the
    # same deserialization failure for this match's document once read
    # back by Rust). sources/threesixtyfive.py's fetch_commentary()
    # already produces this exact shape (minus createdAt, added below).

    def add_commentary(self, match_id: str, entry: Dict) -> None:
        """Add a commentary entry. `entry` must already match
        CommentaryEntry's shape: minute (int), text (str), type (str),
        team (optional str), player (optional str), createdAt (RFC3339 str
        or compatible). createdAt is overwritten here to "now" regardless
        of what's passed in, matching the Rust add_commentary handler's
        own behavior (it does `entry.created_at = now` server-side too)."""
        now = datetime.now(timezone.utc)
        entry = dict(entry)
        entry["createdAt"] = now

        self._collection.update_one(
            {"matchId": match_id},
            {
                "$push": {"commentary": entry},
                "$inc": {"commentaryCount": 1},
                "$set": {"lastCommentaryAt": now, "scrapedAt": now},
            },
            upsert=False,  # NOTE: was upsert=True. This was the actual root
            # cause of zombie fixture documents appearing after archival --
            # 365Scores' commentary/pbp feed keeps producing entries for a
            # match for a while after full-time, so this call was still
            # firing after move_completed_to_history had already deleted the
            # real document, silently recreating a partial stub (matchId +
            # commentary + commentaryCount + lastCommentaryAt + scrapedAt,
            # with an auto-generated ObjectId _id instead of matchId).
            # Commentary should never be able to create a fixture document.
        )

    def add_commentary_bulk(self, match_id: str, entries: List[Dict]) -> None:
        """Add multiple commentary entries. Each entry must already match
        CommentaryEntry's shape (see add_commentary docstring)."""
        now = datetime.now(timezone.utc)
        entries = [dict(e) for e in entries]
        for entry in entries:
            entry["createdAt"] = now

        self._collection.update_one(
            {"matchId": match_id},
            {
                "$push": {"commentary": {"$each": entries}},
                "$inc": {"commentaryCount": len(entries)},
                "$set": {"lastCommentaryAt": now, "scrapedAt": now},
            },
            upsert=False,  # NOTE: was upsert=True -- see add_commentary above
            # for why. Same zombie-doc mechanism, bulk variant.
        )

    def get_commentary(self, match_id: str, limit: int = 50) -> List[Dict]:
        """Get commentary for a match, sorted by minute."""
        pipeline = [
            {"$match": {"matchId": match_id}},
            {"$unwind": "$commentary"},
            {"$sort": {"commentary.minute": 1}},
            {"$limit": limit},
            {"$project": {"commentary": 1, "_id": 0}}
        ]
        result = list(self._collection.aggregate(pipeline))
        return [r["commentary"] for r in result]

    def get_latest_commentary(self, match_id: str, limit: int = 20) -> List[Dict]:
        """Get latest commentary for a match."""
        pipeline = [
            {"$match": {"matchId": match_id}},
            {"$unwind": "$commentary"},
            {"$sort": {"commentary.createdAt": -1}},
            {"$limit": limit},
            {"$project": {"commentary": 1, "_id": 0}}
        ]
        result = list(self._collection.aggregate(pipeline))
        return [r["commentary"] for r in result]

    # ============================================================
    # MATCH FINALIZATION
    # ============================================================

    def finalize_match(self, match_id: str, result: str, home_score: int, away_score: int) -> None:
        """Finalize a match with its result."""
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$set": {
                    "status": "completed",
                    "isLive": False,
                    "availableForVoting": False,
                    "homeScore": home_score,
                    "awayScore": away_score,
                    "result": result,
                    "completedAt": datetime.now(timezone.utc),
                    "scrapedAt": datetime.now(timezone.utc),
                }
            }
        )

    def move_to_history(self, match_id: str) -> None:
        """Mark a match as moved to history."""
        self._collection.update_one(
            {"matchId": match_id},
            {"$set": {"movedToHistory": True}}
        )

    def archive_completed_fixtures(self, hours: int = 24) -> int:
        """Archive completed fixtures older than N hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = self._collection.update_many(
            {
                "status": "completed",
                "completedAt": {"$lt": cutoff},
                "movedToHistory": False,
            },
            {"$set": {"movedToHistory": True, "archivedAt": datetime.now(timezone.utc)}}
        )
        return result.modified_count

    # ============================================================
    # VOTERS & USER DATA
    # ============================================================
    # NOTE: Rust's Voter struct requires userId, userName, selection,
    # votedAt (camelCase, via #[serde(rename)]). The voter dict built here
    # must match that exactly or this field will fail deserialization too.

    def add_voter(self, match_id: str, user_id: str, user_name: str, selection: str) -> None:
        """Add a voter to a match. Matches Rust's Voter struct shape
        exactly: userId, userName, selection, votedAt."""
        voter = {
            "userId": user_id,
            "userName": user_name,
            "selection": selection,
            "votedAt": datetime.now(timezone.utc),
        }
        self._collection.update_one(
            {"matchId": match_id},
            {
                "$push": {"voters": voter},
                "$inc": {"votes": 1},
            },
            upsert=True,
        )

    def get_voters(self, match_id: str) -> List[Dict]:
        """Get all voters for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"voters": 1}
        )
        return doc.get("voters", []) if doc else []

    def get_vote_count(self, match_id: str) -> int:
        """Get the vote count for a match."""
        doc = self._collection.find_one(
            {"matchId": match_id},
            {"votes": 1}
        )
        return doc.get("votes", 0) if doc else 0

    def user_has_voted(self, match_id: str, user_id: str) -> bool:
        """Check if a user has voted on a match."""
        doc = self._collection.find_one({
            "matchId": match_id,
            "voters.userId": user_id
        })
        return doc is not None

    # ============================================================
    # BULK OPERATIONS
    # ============================================================

    def upsert_fixtures_bulk(self, fixtures: List[Dict]) -> int:
        """Bulk upsert fixtures. CAUTION: each fixture dict is written
        as-is via replace_one -- callers must ensure dicts already use
        camelCase keys matching the Game struct (e.g. via upsert_fixture's
        doc-building logic), or this bypasses the schema entirely."""
        operations = []
        for fixture in fixtures:
            match_id = fixture.get("matchId")
            if match_id:
                operations.append(
                    {
                        "replace_one": {
                            "filter": {"matchId": match_id},
                            "replacement": fixture,
                            "upsert": True,
                        }
                    }
                )

        if operations:
            result = self._collection.bulk_write(operations)
            return result.upserted_count + result.modified_count
        return 0

    # ============================================================
    # CLEANUP
    # ============================================================

    def delete_old_fixtures(self, days: int = 30) -> int:
        """Delete fixtures older than N days (that are archived)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = self._collection.delete_many({
            "movedToHistory": True,
            "completedAt": {"$lt": cutoff},
        })
        return result.deleted_count

    def close(self) -> None:
        """Close the MongoDB connection."""
        self._client.close()

    # ============================================================
    # AGGREGATION HELPERS
    # ============================================================

    def get_fixture_counts_by_status(self) -> Dict[str, int]:
        """Get count of fixtures by status."""
        pipeline = [
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]
        results = list(self._collection.aggregate(pipeline))
        return {r["_id"]: r["count"] for r in results}

    def get_upcoming_fixtures_with_lineups(self) -> List[Dict]:
        """Get upcoming fixtures that have lineups available."""
        return list(self._collection.find({
            "status": {"$in": ["upcoming", "soon"]},
            "lineupsFetched": True
        }))

    def get_live_fixtures_with_stats(self) -> List[Dict]:
        """Get live fixtures that have statistics."""
        return list(self._collection.find({
            "status": "live",
            "statistics": {"$exists": True, "$ne": []}
        }))


def create_store(mongo_uri: str = None) -> FixtureStore:
    """Create a FixtureStore instance with optional URI."""
    import os
    if mongo_uri is None:
        mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        raise ValueError("MONGO_URI environment variable is required")
    return FixtureStore(mongo_uri)