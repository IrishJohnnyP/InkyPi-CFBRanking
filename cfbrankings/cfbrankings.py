import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

# Base endpoints
SITE_API_RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"
# CFP workaround provided by user: site.web.api + type=2
WEB_API_RANKINGS_URL = "https://site.web.api.espn.com/apis/site/v2/sports/football/college-football/rankings"


class CfbRankings(BasePlugin):
    """College Football Rankings (AP / Coaches / CFP)

    CFP support:
      - Uses ESPN web API endpoint with required parameter ?type=2 for CFP.
      - Optional year parameter (e.g., year=2024) supported via plugin setting.

    Display:
      - Font size selector: normal / large / larger / largest
      - Two-column layout whenever Top N > 15
      - Records auto-hidden when Top N > 20
      - Team line shows logo + School (Nickname) with nickname styled smaller
    """

    _cache: Dict[str, Any] = {"ts": {}, "data": {}}

    def generate_settings_template(self):
        params = super().generate_settings_template()
        params["style_settings"] = True
        return params

    def generate_image(self, settings: Dict[str, Any], device_config):
        poll_choice = (settings.get("poll") or "auto").strip().lower()
        top_n = max(1, min(25, int(settings.get("top_n") or 20)))

        font_size = (settings.get("font_size") or "normal").strip().lower()
        if font_size not in ("normal", "large", "larger", "largest"):
            font_size = "normal"

        # Optional year (mainly for CFP endpoint)
        year = self._parse_year(settings.get("year"))

        show_record_user = self._to_bool(settings.get("show_record", True))
        show_meta = self._to_bool(settings.get("show_meta", True))
        show_record = bool(show_record_user and top_n <= 20)

        cache_minutes = max(0, min(1440, int(settings.get("cache_minutes") or 30)))
        ttl = cache_minutes * 60

        dimensions = self._get_dimensions(settings, device_config)

        # Two-column layout whenever Top N > 15
        two_column = top_n > 15

        data, poll = self._get_poll_data(poll_choice, year, ttl)

        poll_name = (poll.get("name") or poll.get("shortName") or "College Football Rankings").strip()
        title = poll_name

        ranks = self._extract_ranks(poll)
        rows = self._build_rows(ranks, top_n, show_record)

        meta = ""
        if show_meta:
            season = (data.get("season") or data.get("requestedSeason") or data.get("currentSeason") or {}).get("year")
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
            "note": "",
            "rows": rows,
            "show_record": show_record,
            "two_column": two_column,
            "top_n": top_n,
            "font_size": font_size,
            "plugin_settings": settings,
        }

        return self.render_image(dimensions, "cfbrankings.html", "cfbrankings.css", template_params)

    # ----------------------------
    # Poll fetching
    # ----------------------------

    def _get_poll_data(self, poll_choice: str, year: Optional[int], ttl: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if poll_choice == "cfp":
            # Required: type=2 per user
            url = f"{WEB_API_RANKINGS_URL}?type=2"
            if year is not None:
                url += f"&year={year}"

            data = self._fetch_json_cached(url, ttl)
            poll = self._extract_poll_from_response(data)
            if poll is None:
                raise RuntimeError("CFP poll not found in ESPN web API response.")
            return data, poll

        # AP/Coaches/Auto from site.api endpoint
        data = self._fetch_json_cached(SITE_API_RANKINGS_URL, ttl)
        poll = self._select_poll(data, poll_choice)
        if poll is None:
            raise RuntimeError("No suitable poll found in ESPN response.")
        return data, poll

    def _fetch_json_cached(self, url: str, ttl: int) -> Dict[str, Any]:
        now = time.time()
        ts = self._cache["ts"].get(url, 0.0)
        if ttl > 0 and url in self._cache["data"] and (now - ts) < ttl:
            return self._cache["data"][url]

        session = get_http_session()
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        if ttl > 0:
            self._cache["ts"][url] = now
            self._cache["data"][url] = data

        return data

    def _extract_poll_from_response(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """CFP endpoint may return either a poll object or a container with rankings."""
        if not isinstance(data, dict):
            return None

        # If response itself looks like a poll (has ranks)
        if "ranks" in data and isinstance(data.get("ranks"), (list, dict)):
            return data

        rankings = data.get("rankings")
        if isinstance(rankings, dict):
            rankings = rankings.get("items") or rankings.get("rankings")
        if isinstance(rankings, list) and rankings:
            # Often the response is a list of polls; take the first
            first = rankings[0]
            return first if isinstance(first, dict) else None

        # Sometimes nested under 'ranking'
        ranking = data.get("ranking")
        if isinstance(ranking, dict) and "ranks" in ranking:
            return ranking

        return None

    def _extract_ranks(self, poll: Dict[str, Any]) -> List[Dict[str, Any]]:
        ranks = poll.get("ranks")
        if isinstance(ranks, dict):
            ranks = ranks.get("items") or ranks.get("entries") or ranks.get("ranks")
        if not isinstance(ranks, list):
            ranks = poll.get("entries") or []
        if not isinstance(ranks, list):
            ranks = []
        # Ensure list of dicts
        return [r for r in ranks if isinstance(r, dict)]

    # ----------------------------
    # Poll selection (AP/Coaches/Auto)
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
                    ds = str(v).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ds)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except Exception:
                    pass
            return 0.0

        def kind(p: Dict[str, Any]) -> str:
            t = str(p.get("type") or "").lower()
            if t in ("ap", "coaches"):
                return t
            name = str(p.get("name") or "").lower()
            short = str(p.get("shortName") or p.get("abbreviation") or "").lower()
            head = str(p.get("headline") or "").lower()
            blob = " ".join([name, short, head])
            if "ap" in blob and "poll" in blob:
                return "ap"
            if "coaches" in blob or "afca" in blob:
                return "coaches"
            return ""

        if choice in ("ap", "coaches"):
            matches = [p for p in polls if kind(p) == choice]
            if not matches:
                return None
            matches.sort(key=parse_date, reverse=True)
            return matches[0]

        # AUTO
        order = {"ap": 2, "coaches": 1}
        candidates = [(parse_date(p), order.get(kind(p), 0), p) for p in polls if kind(p) in order]
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return candidates[0][2]

        polls.sort(key=parse_date, reverse=True)
        return polls[0]

    # ----------------------------
    # Date formatting
    # ----------------------------

    def _get_tzinfo(self, device_config):
        try:
            tz_name = device_config.get_config("timezone")
        except Exception:
            tz_name = None
        if not tz_name:
            return None
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception:
            return None

    def _format_poll_date(self, poll: Dict[str, Any], poll_name: str, device_config) -> str:
        from datetime import datetime, timezone
        date_str = None
        for k in ("date", "lastUpdated", "lastUpdate", "updated", "updateDate"):
            v = poll.get(k)
            if v:
                date_str = str(v)
                break
        if not date_str:
            return ""

        tzinfo = self._get_tzinfo(device_config)
        try:
            ds = date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ds)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(tzinfo) if tzinfo else dt.astimezone()

            label = (poll_name or "").strip()
            hour = dt_local.strftime("%I").lstrip("0") or "12"
            minute = dt_local.strftime("%M")
            ampm = dt_local.strftime("%p")
            tz_abbr = (dt_local.strftime("%Z") or "").strip()
            date_part = dt_local.strftime("%b %d, %Y")
            time_part = f"{hour}:{minute} {ampm}"
            if tz_abbr:
                time_part = f"{time_part} {tz_abbr}"
            if label:
                return f"Updated {date_part} {time_part} • {label}"
            return f"Updated {date_part} {time_part}"
        except Exception:
            label = (poll_name or "").strip()
            if label:
                return f"Updated {date_str} • {label}"
            return f"Updated {date_str}"

    # ----------------------------
    # Rows
    # ----------------------------

    def _build_rows(self, ranks: List[Dict[str, Any]], top_n: int, show_record: bool):
        rows = []
        for entry in ranks[:top_n]:
            rk = entry.get("current") or entry.get("rank") or entry.get("position") or entry.get("ranking")
            team = entry.get("team") or entry.get("school") or {}
            if not isinstance(team, dict):
                team = {}

            school = team.get("shortDisplayName") or team.get("location") or team.get("displayName") or team.get("abbreviation") or team.get("name") or "Unknown"
            nickname = team.get("name") or team.get("nickname") or ""

            nick_out = ""
            if nickname and nickname.lower() not in school.lower():
                nick_out = nickname

            logo = ""
            logos = team.get("logos")
            if isinstance(logos, list) and logos:
                href = None
                for item in logos:
                    if not isinstance(item, dict):
                        continue
                    rel = item.get("rel")
                    if isinstance(rel, list) and "default" in rel and item.get("href"):
                        href = item.get("href")
                        break
                if not href:
                    for item in logos:
                        if isinstance(item, dict) and item.get("href"):
                            href = item.get("href")
                            break
                logo = href or ""

            rec = entry.get("recordSummary") or entry.get("record") or ""

            rows.append({
                "rank": rk if rk is not None else "--",
                "school": school,
                "nickname": nick_out,
                "logo": logo,
                "record": rec if show_record else "",
            })
        return rows

    # ----------------------------
    # Utilities
    # ----------------------------

    def _get_dimensions(self, settings: Dict[str, Any], device_config) -> Tuple[int, int]:
        screen_size = (settings.get("screen_size") or "auto").strip().lower()
        if screen_size == "800x480":
            dims = (800, 480)
        elif screen_size == "1600x1200":
            dims = (1600, 1200)
        else:
            dims = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dims = dims[::-1]
        return dims

    def _to_bool(self, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "checked")
        return bool(v)

    def _parse_year(self, v: Any) -> Optional[int]:
        if v is None:
            return None
        try:
            s = str(v).strip()
            if not s:
                return None
            year = int(s)
            if 2000 <= year <= 2100:
                return year
        except Exception:
            return None
        return None
