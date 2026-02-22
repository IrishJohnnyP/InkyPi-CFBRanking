import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

# Primary ESPN endpoint for CFB rankings (unofficial/undocumented, may change)
ESPN_RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"


class CfbRankings(BasePlugin):
    """College Football Rankings (AP / Coaches / CFP)

    Key behavior:
      - CFP selection is handled by trying multiple ESPN URL variants so it works when ESPN exposes CFP
        via a different query shape.
      - If CFP is not currently published by ESPN, a clear RuntimeError message is shown.
      - Two-column layout is enabled whenever Top N > 15.
      - Team line shows logo + School (Nickname).
      - Uses HTML/CSS render pipeline (style settings enabled).
    """

    # Cache per URL to avoid hammering ESPN
    _cache: Dict[str, Any] = {"ts": {}, "data": {}}

    def generate_settings_template(self):
        params = super().generate_settings_template()
        params["style_settings"] = True
        return params

    def generate_image(self, settings: Dict[str, Any], device_config):
        poll_choice = (settings.get("poll") or "auto").strip().lower()
        top_n = max(1, min(25, int(settings.get("top_n") or 20)))

        show_record_user = self._to_bool(settings.get("show_record", True))
        show_meta = self._to_bool(settings.get("show_meta", True))
        show_record = bool(show_record_user and top_n <= 20)

        cache_minutes = max(0, min(1440, int(settings.get("cache_minutes") or 30)))
        ttl = cache_minutes * 60

        dimensions = self._get_dimensions(settings, device_config)

        # Two-column layout whenever Top N > 15
        two_column = top_n > 15

        try:
            data, poll, note = self._get_selected_poll_data(poll_choice, ttl)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to fetch rankings: {e}")

        poll_name = (poll.get("name") or poll.get("shortName") or "College Football Rankings").strip()
        title = poll_name

        # Extract ranks robustly
        ranks = poll.get("ranks")
        if isinstance(ranks, dict):
            ranks = ranks.get("items") or ranks.get("entries") or ranks.get("ranks")
        if not isinstance(ranks, list):
            ranks = poll.get("entries") or []
        if not isinstance(ranks, list):
            ranks = []

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
            "note": note,
            "rows": rows,
            "show_record": show_record,
            "two_column": two_column,
            "top_n": top_n,
            "plugin_settings": settings,
        }

        return self.render_image(dimensions, "cfbrankings.html", "cfbrankings.css", template_params)

    # ----------------------------
    # CFP-safe poll fetching
    # ----------------------------

    def _get_selected_poll_data(self, poll_choice: str, ttl: int) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
        """Returns (data_json, poll_obj, note)."""
        # Always fetch base first for AP/Coaches/Auto
        if poll_choice in ("auto", "ap", "coaches"):
            data = self._fetch_json_cached(ESPN_RANKINGS_URL, ttl)
            poll = self._select_poll(data, poll_choice)
            if not poll:
                raise RuntimeError("No suitable poll found in ESPN response.")
            return data, poll, ""

        # CFP: try multiple URL variants because ESPN sometimes varies how CFP is exposed.
        if poll_choice == "cfp":
            urls = [
                ESPN_RANKINGS_URL,
                ESPN_RANKINGS_URL + "?type=cfp",
                ESPN_RANKINGS_URL + "?types=cfp",
                ESPN_RANKINGS_URL + "?poll=cfp",
                ESPN_RANKINGS_URL + "?rankings=cfp",
                ESPN_RANKINGS_URL + "?seasontype=2",
                ESPN_RANKINGS_URL + "?seasontype=3",
            ]

            # First pass: try to find a CFP poll in any response.
            for url in urls:
                data = self._fetch_json_cached(url, ttl)
                poll = self._select_poll(data, "cfp")
                if poll:
                    note = "" if url == ESPN_RANKINGS_URL else ""
                    return data, poll, note

            # Second pass: CFP not available -> raise a clearer error.
            raise RuntimeError(
                "CFP ranking is not available from ESPN right now. "
                "This typically means the selection committee rankings are not currently published for the active season/week."
            )

        raise RuntimeError("Invalid poll selection.")

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
                    ds = str(v).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ds)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except Exception:
                    pass
            return 0.0

        def kind(p: Dict[str, Any]) -> str:
            # Prefer stable type when present
            t = str(p.get("type") or "").lower()
            if t in ("ap", "coaches", "cfp"):
                return t

            # Otherwise infer from name/shortName/headline
            name = str(p.get("name") or "").lower()
            short = str(p.get("shortName") or p.get("abbreviation") or "").lower()
            head = str(p.get("headline") or "").lower()
            blob = " ".join([name, short, head])

            if "playoff" in blob or "cfp" in blob or "selection committee" in blob:
                return "cfp"
            if "ap" in blob and "poll" in blob:
                return "ap"
            if "coaches" in blob or "afca" in blob:
                return "coaches"
            return ""

        if choice in ("ap", "coaches", "cfp"):
            matches = [p for p in polls if kind(p) == choice]
            if not matches:
                return None
            matches.sort(key=parse_date, reverse=True)
            return matches[0]

        # AUTO: most recent among known kinds (tie-break CFP > AP > Coaches)
        order = {"cfp": 3, "ap": 2, "coaches": 1}
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

            label = (poll_name or "").strip() or (poll.get("shortName") or "").strip() or (poll.get("name") or "").strip()
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
            label = (poll_name or "").strip() or (poll.get("shortName") or "").strip() or (poll.get("name") or "").strip()
            if label:
                return f"Updated {date_str} • {label}"
            return f"Updated {date_str}"

    # ----------------------------
    # Rows (logo + School (Nickname))
    # ----------------------------

    def _build_rows(self, ranks: List[Dict[str, Any]], top_n: int, show_record: bool):
        rows = []
        if isinstance(ranks, dict):
            ranks = ranks.get("items") or ranks.get("entries") or ranks.get("ranks") or []
        if not isinstance(ranks, list):
            ranks = []

        for entry in ranks[:top_n]:
            if not isinstance(entry, dict):
                continue
            rk = entry.get("current") or entry.get("rank") or entry.get("position") or entry.get("ranking")
            team = entry.get("team") or entry.get("school") or {}
            if not isinstance(team, dict):
                team = {}

            school = team.get("shortDisplayName") or team.get("location") or team.get("displayName") or team.get("abbreviation") or team.get("name") or "Unknown"
            nickname = team.get("name") or team.get("nickname") or ""

            display = school
            if nickname and nickname.lower() not in school.lower():
                display = f"{school} ({nickname})"

            # Logo URL
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
                "team": display,
                "logo": logo,
                "record": rec if show_record else "",
            })

        return rows

    # ----------------------------
    # Dimensions / orientation
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
