import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

ESPN_RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"


class CfbRankings(BasePlugin):
    """College Football Rankings plugin (AP / Coaches / CFP)

    Features:
      - Poll selectable: auto / AP / Coaches / CFP
      - Top N selectable (1-25)
      - Render size selectable: auto / 800x480 / 1600x1200
      - Style settings enabled (background, font colors, margins, etc.)
      - Two-column layout automatically on small screens ONLY when records are hidden and Top N > 15
      - Records automatically hidden when Top N > 20 to prevent crowding
      - Displays poll update date in local time, including poll name (e.g., "Updated 2026-02-21 15:36 • AP Top 25")
    """

    _cache: Dict[str, Any] = {"ts": 0.0, "key": None, "data": None}

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        # Enable built-in style settings in the Web UI
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings: Dict[str, Any], device_config):
        poll_choice = (settings.get("poll") or "auto").strip().lower()

        top_n = int(settings.get("top_n") or 20)
        top_n = max(1, min(25, top_n))

        show_record_user = self._to_bool(settings.get("show_record", True))
        show_meta = self._to_bool(settings.get("show_meta", True))

        # Automatically hide record column when Top N > 20 (even if user enabled it)
        show_record = bool(show_record_user and top_n <= 20)

        cache_minutes = int(settings.get("cache_minutes") or 30)
        cache_minutes = max(0, min(1440, cache_minutes))
        ttl = cache_minutes * 60

        dimensions = self._get_dimensions(settings, device_config)

        # Auto two-column ONLY when records are hidden and Top N > 15 on small screens
        small_screen = max(dimensions) <= 800
        two_column = bool((not show_record) and top_n > 15 and small_screen)

        try:
            data = self._get_rankings_cached(ttl)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch rankings: {e}")

        poll = self._select_poll(data, poll_choice)
        if not poll:
            raise RuntimeError("No suitable poll found in ESPN response.")

        poll_name = poll.get("name") or poll.get("shortName") or ""
        title = poll_name or "College Football Rankings"

        ranks = poll.get("ranks") or []
        if isinstance(ranks, dict):
            ranks = ranks.get("items") or []

        rows = self._build_rows(ranks, top_n, show_record)

        meta = ""
        if show_meta:
            season = (data.get("season") or {}).get("year")
            week = (data.get("week") or {}).get("number")
            if season and week:
                meta = f"Season {season} • Week {week}"
            elif season:
                meta = f"Season {season}"

        poll_date = self._format_poll_date(poll, poll_name, device_config)

        template_params = {
            "title": title,
            "meta": meta,
            "poll_date": poll_date,
            "rows": rows,
            "show_record": show_record,
            "two_column": two_column,
            "top_n": top_n,
            # IMPORTANT: pass plugin_settings so style settings from the UI can be applied
            "plugin_settings": settings,
        }

        return self.render_image(dimensions, "cfbrankings.html", "cfbrankings.css", template_params)

    # ----------------------------
    # Data fetching + caching
    # ----------------------------

    def _get_rankings_cached(self, ttl: int) -> Dict[str, Any]:
        now = time.time()
        key = "rankings"

        if ttl > 0 and self._cache["data"] is not None:
            if self._cache["key"] == key and (now - self._cache["ts"]) < ttl:
                return self._cache["data"]

        session = get_http_session()
        resp = session.get(ESPN_RANKINGS_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        if ttl > 0:
            self._cache.update({"ts": now, "key": key, "data": data})

        return data

    # ----------------------------
    # Poll date formatting
    # ----------------------------

    def _get_tzinfo(self, device_config):
        """Best-effort tzinfo: prefer device_config timezone, else system local."""
        try:
            tz_name = device_config.get_config("timezone")
        except Exception:
            tz_name = None

        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                return ZoneInfo(tz_name)
            except Exception:
                return None
        return None

    def _format_poll_date(self, poll: Dict[str, Any], poll_name: str, device_config) -> str:
        """Best-effort poll update date formatted for display in *local time*.

        Preferred format (with TZ abbreviation):
            Updated Feb 21, 2026 3:36 PM EST • AP Top 25

        Fallback when TZ abbreviation (%Z) is empty:
            Updated Feb 21, 2026 3:36 PM • AP Top 25
        """
        from datetime import datetime, timezone

        date_str = None
        for k in ("date", "lastUpdated", "lastUpdate", "updated", "updateDate"):
            v = poll.get(k)
            if v:
                date_str = v
                break

        if not date_str:
            return ""

        tzinfo = self._get_tzinfo(device_config)

        try:
            ds = date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ds)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            # Convert to configured timezone, else system local timezone
            dt_local = dt.astimezone(tzinfo) if tzinfo is not None else dt.astimezone()

            label = (poll_name or "").strip() or (poll.get("shortName") or "").strip() or (poll.get("name") or "").strip()

            # 12-hour time without leading zero
            hour = dt_local.strftime("%I").lstrip("0") or "12"
            minute = dt_local.strftime("%M")
            ampm = dt_local.strftime("%p")
            tz_abbr = (dt_local.strftime("%Z") or "").strip()

            date_part = dt_local.strftime("%b %d, %Y")
            time_part = f"{hour}:{minute} {ampm}"

            # Only include TZ abbreviation when present; otherwise use fallback without TZ.
            if tz_abbr:
                time_part = f"{time_part} {tz_abbr}"

            if label:
                return f"Updated {date_part} {time_part} • {label}"
            return f"Updated {date_part} {time_part}"

        except Exception:
            label = (poll_name or "").strip() or (poll.get("shortName") or "").strip() or (poll.get("name") or "").strip()
            if label:
                return f"Updated {date_str} • {label}"
            return f"Updated {date_str}"



    # ----------------------------
    # Poll selection
    # ----------------------------

    def _select_poll(self, data: Dict[str, Any], choice: str) -> Optional[Dict[str, Any]]:
        from datetime import datetime, timezone

        polls = data.get("rankings")
        if isinstance(polls, dict):
            polls = polls.get("items") or polls.get("rankings")
        if not isinstance(polls, list) or not polls:
            return None

        def parse_date(p: Dict[str, Any]) -> float:
            for k in ("date", "lastUpdated", "lastUpdate", "updated", "updateDate"):
                v = p.get(k)
                if not v:
                    continue
                try:
                    ds = v.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ds)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except Exception:
                    pass
            return 0.0

        def match_score(p: Dict[str, Any], target: str) -> int:
            name = (p.get("name") or "").lower()
            short = (p.get("shortName") or p.get("abbreviation") or "").lower()

            if target == "ap":
                if short == "ap" or "ap top 25" in name or name.startswith("ap "):
                    return 10
                if "ap" in name:
                    return 6
            if target == "coaches":
                if short == "coaches" or "coaches poll" in name or "afca" in name:
                    return 10
                if "coaches" in name:
                    return 6
            if target == "cfp":
                if short == "cfp" or "college football playoff" in name or "playoff" in name:
                    return 10
                if "playoff" in name:
                    return 6
            return 0

        # Specific poll selection
        if choice in ("ap", "coaches", "cfp"):
            best = None
            best_score = 0
            best_date = 0.0
            for p in polls:
                if not isinstance(p, dict):
                    continue
                s = match_score(p, choice)
                if s <= 0:
                    continue
                d = parse_date(p)
                if s > best_score or (s == best_score and d > best_date):
                    best, best_score, best_date = p, s, d
            return best

        # AUTO: most recent among known polls
        candidates: List[Tuple[float, int, Dict[str, Any]]] = []
        for p in polls:
            if not isinstance(p, dict):
                continue
            best_type_score = max(match_score(p, "cfp"), match_score(p, "ap"), match_score(p, "coaches"))
            if best_type_score <= 0:
                continue
            d = parse_date(p)
            candidates.append((d, best_type_score, p))

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return candidates[0][2]

        return polls[0] if polls else None

    def _build_rows(self, ranks: List[Dict[str, Any]], top_n: int, show_record: bool):
        rows = []
        for entry in ranks[:top_n]:
            rk = entry.get("current") or entry.get("rank") or entry.get("position")
            team = entry.get("team") or {}
            team_name = (
                team.get("shortDisplayName")
                or team.get("displayName")
                or team.get("name")
                or "Unknown"
            )
            rec = entry.get("recordSummary") or entry.get("record") or ""
            rows.append({
                "rank": rk if rk is not None else "--",
                "team": team_name,
                "record": rec if show_record else "",
            })
        return rows

    # ----------------------------
    # Dimensions / orientation
    # ----------------------------

    def _get_dimensions(self, settings: Dict[str, Any], device_config) -> Tuple[int, int]:
        screen_size = (settings.get("screen_size") or "auto").strip().lower()

        if screen_size == "800x480":
            dimensions = (800, 480)
        elif screen_size == "1600x1200":
            dimensions = (1600, 1200)
        else:
            dimensions = device_config.get_resolution()

        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        return dimensions

    def _to_bool(self, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "checked")
        return bool(v)
