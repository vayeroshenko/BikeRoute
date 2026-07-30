from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st

from idf_commute.cli import (
    _bike_state_store,
    _clean_alert_message,
    _confirm_outbound_selection,
    _confirm_return_selection,
    _run_outbound_plan,
    _run_return_plan,
)
from idf_commute.config import AppConfig, load_config
from idf_commute.domain.models import (
    OutboundOption,
    OutboundOptionKind,
    ReliabilityAssessment,
    TransitLeg,
)
from idf_commute.domain.state import BikeLocation, BikeState
from idf_commute.planning.models import OutboundPlan, ReturnOption, ReturnPlan
from idf_commute.planning.planner import (
    longest_walking_leg_minutes,
    walking_duration_minutes,
)
from idf_commute.planning.scoring import ScoreMode

CONFIG_ENV = "IDF_COMMUTE_CONFIG"


def main() -> None:
    st.set_page_config(
        page_title="IDF Commute",
        page_icon="🚲",
        layout="wide",
    )
    _style()
    st.title("IDF Commute")
    st.caption("Bike to transit, with the bicycle kept at the correct station.")

    config_path = Path(os.environ.get(CONFIG_ENV, "config.yaml"))
    try:
        config = load_config(config_path)
        store = _bike_state_store(config_path)
        bike_state = store.load()
    except Exception as exc:
        st.error(f"Configuration could not be loaded: {exc}")
        st.stop()

    _state_sidebar(config_path, bike_state)
    if message := st.session_state.pop("flash_message", None):
        st.success(str(message))

    direction = st.radio(
        "Journey",
        ("Outbound", "Return"),
        horizontal=True,
        help="Return always retrieves the bicycle from its stored station.",
    )
    departure = _planning_controls(config, direction)
    if departure is None:
        _render_saved_plan(config_path, direction)
        return

    (
        departure_text,
        max_bike_minutes,
        max_walking_minutes,
        max_walking_leg_minutes,
        max_results,
        score_mode,
        station_selection,
        station_ranges,
        max_api_requests,
    ) = departure

    try:
        with st.spinner("Requesting current routes and disruptions…"):
            if direction == "Outbound":
                plan: OutboundPlan | ReturnPlan = asyncio.run(
                    _run_outbound_plan(
                        config_path,
                        departure_text,
                        max_bike_minutes,
                        max_walking_minutes,
                        max_walking_leg_minutes,
                        max_results,
                        station_selection,
                        station_ranges,
                        score_mode,
                        max_api_requests,
                    )
                )
            else:
                plan = asyncio.run(
                    _run_return_plan(
                        config_path,
                        departure_text,
                        max_bike_minutes,
                        max_walking_minutes,
                        max_walking_leg_minutes,
                        max_results,
                        score_mode,
                        max_api_requests,
                    )
                )
    except Exception as exc:
        st.error(f"Planning stopped: {exc}")
        return
    st.session_state["plan"] = plan
    st.session_state["plan_direction"] = direction
    _render_saved_plan(config_path, direction)


def _planning_controls(
    config: AppConfig,
    direction: str,
) -> (
    tuple[
        str,
        float,
        float,
        float,
        int,
        ScoreMode,
        str | None,
        list[str] | None,
        int,
    ]
    | None
):
    zone = ZoneInfo(config.timezone)
    now = datetime.now(zone)
    default_time = now.replace(second=0, microsecond=0).time()
    with st.form("planning"):
        date_column, time_column, mode_column = st.columns((1, 1, 1.25))
        travel_date = date_column.date_input("Date", value=now.date())
        travel_time = time_column.time_input("Departure", value=default_time)
        score_mode = ScoreMode(
            mode_column.selectbox(
                "Ranking mode",
                [mode.value for mode in ScoreMode],
                index=list(ScoreMode).index(config.scoring.mode),
            )
        )

        bike_column, walk_column, leg_column, count_column = st.columns(4)
        max_bike_minutes = float(
            bike_column.number_input(
                "Maximum bike minutes",
                min_value=1.0,
                max_value=180.0,
                value=float(config.bicycle.max_bike_minutes),
                step=1.0,
            )
        )
        max_walking_minutes = float(
            walk_column.number_input(
                "Maximum walking total",
                min_value=1.0,
                max_value=180.0,
                value=float(config.walking.max_minutes),
                step=1.0,
            )
        )
        max_walking_leg_minutes = float(
            leg_column.number_input(
                "Maximum single walk",
                min_value=1.0,
                max_value=120.0,
                value=float(config.walking.max_leg_minutes),
                step=1.0,
            )
        )
        max_results = int(
            count_column.number_input(
                "Results",
                min_value=1,
                max_value=20,
                value=10,
                step=1,
            )
        )

        station_selection: str | None = None
        station_ranges: list[str] | None = None
        if direction == "Outbound":
            station_selection, station_ranges = _outbound_station_controls()
        else:
            st.caption("The return destination is locked to the station in bicycle state.")
        max_api_requests = int(
            st.number_input(
                "PRIM request budget",
                min_value=1,
                max_value=200,
                value=config.reliability.max_requests_per_plan,
                help="Counts real HTTP attempts, including retries. Cache hits do not count.",
            )
        )
        submitted = st.form_submit_button(
            "Plan current journey",
            type="primary",
            use_container_width=True,
        )
    if not submitted:
        return None
    if direction == "Outbound" and station_selection == "":
        st.error("Enter a station name or stop-area ID.")
        return None
    departure = datetime.combine(
        _as_date(travel_date),
        _as_time(travel_time),
        tzinfo=zone,
    )
    return (
        departure.isoformat(),
        max_bike_minutes,
        max_walking_minutes,
        max_walking_leg_minutes,
        max_results,
        score_mode,
        station_selection,
        station_ranges,
        max_api_requests,
    )


def _outbound_station_controls() -> tuple[str | None, list[str] | None]:
    mode = st.radio(
        "Bicycle station search",
        ("Best RER B", "Configured candidates", "One RER B station"),
        horizontal=True,
    )
    if mode == "Configured candidates":
        return None, None
    if mode == "One RER B station":
        station = st.text_input(
            "Station name or stop-area ID",
            placeholder="Bourg-la-Reine",
        ).strip()
        return station, None
    ranges_text = st.text_area(
        "Optional RER B ranges — one START..END path per line",
        placeholder="Laplace..Bourg-la-Reine\nBourg-la-Reine..Sceaux",
        help="Leave empty to use bicycle.best_station_ranges from config.yaml.",
    )
    ranges = [line.strip() for line in ranges_text.splitlines() if line.strip()]
    return "best", ranges or None


def _state_sidebar(config_path: Path, state: BikeState) -> None:
    with st.sidebar:
        st.header("Bicycle state")
        if state.location is BikeLocation.STATION:
            st.info(f"Parked at **{state.station_name or state.station_id}**")
            if state.station_name and state.station_id:
                st.caption(state.station_id)
        elif state.location is BikeLocation.HOME:
            st.success("At home")
        else:
            st.warning("Unknown")
        if state.updated_at is not None:
            st.caption(f"Updated {state.updated_at:%Y-%m-%d %H:%M}")

        home_column, unknown_column = st.columns(2)
        if home_column.button("Mark home", use_container_width=True):
            _bike_state_store(config_path).set_home()
            _flash_and_rerun("Bicycle recorded at home.")
        if unknown_column.button("Mark unknown", use_container_width=True):
            _bike_state_store(config_path).set_unknown()
            _flash_and_rerun("Bicycle location cleared.")

        with st.expander("Correct parked station"):
            station_id = st.text_input(
                "Stop-area ID",
                placeholder="stop_area:IDFM:70033",
            )
            station_name = st.text_input("Station name", placeholder="Bourg-la-Reine")
            if st.button("Save parked station", use_container_width=True):
                if not station_id.strip():
                    st.error("A stable stop-area ID is required.")
                else:
                    _bike_state_store(config_path).set_station(
                        station_id.strip(),
                        station_name=station_name.strip() or None,
                    )
                    _flash_and_rerun("Parked station updated.")
        st.divider()
        st.caption(f"Configuration: {config_path}")


def _render_saved_plan(config_path: Path, direction: str) -> None:
    plan = st.session_state.get("plan")
    if plan is None or st.session_state.get("plan_direction") != direction:
        return
    if isinstance(plan, OutboundPlan):
        _render_outbound(plan, config_path)
    elif isinstance(plan, ReturnPlan):
        _render_return(plan, config_path)


def _render_outbound(plan: OutboundPlan, config_path: Path) -> None:
    st.subheader("Outbound comparison")
    st.caption(
        f"PRIM requests used: {plan.api_requests} / "
        f"{plan.api_request_limit or 'unlimited'}"
    )
    if plan.bike_state.location is not BikeLocation.HOME:
        location = (
            plan.bike_state.station_name
            or plan.bike_state.station_id
            or plan.bike_state.location.value
        )
        st.warning(f"Bike options were suppressed because the bicycle location is {location}.")
    st.dataframe(outbound_summary_rows(plan), hide_index=True, use_container_width=True)
    for rank, option in enumerate(plan.options, start=1):
        title = (
            f"#{rank} · bike to {option.station.name}"
            if option.station is not None
            else f"#{rank} · all transit"
        )
        with st.expander(title, expanded=rank == 1):
            _option_metrics(option)
            _reliability_details(option.reliability)
            if option.bike_route is not None:
                _bike_route_details(option.bike_route)
            _transit_details(option.transit_journey.legs)
            _score_details(option.score)
            _alerts(option.matched_disruptions)
            if st.button(f"Confirm option #{rank}", key=f"confirm-outbound-{rank}"):
                try:
                    state = _confirm_outbound_selection(
                        plan,
                        rank,
                        _bike_state_store(config_path),
                    )
                except Exception as exc:
                    st.error(f"Confirmation stopped: {exc}")
                else:
                    message = (
                        "All-transit option confirmed; bicycle state unchanged."
                        if state is None
                        else f"Bicycle recorded at {state.station_name or state.station_id}."
                    )
                    st.session_state.pop("plan", None)
                    _flash_and_rerun(message)
    _rejections(plan.rejections)


def _render_return(plan: ReturnPlan, config_path: Path) -> None:
    station = plan.bike_state.station_name or plan.bike_state.station_id
    st.subheader(f"Return via {station}")
    st.caption(
        f"PRIM requests used: {plan.api_requests} / "
        f"{plan.api_request_limit or 'unlimited'}"
    )
    st.dataframe(return_summary_rows(plan), hide_index=True, use_container_width=True)
    for rank, option in enumerate(plan.options, start=1):
        with st.expander(
            f"#{rank} · retrieve at {option.station.name}",
            expanded=rank == 1,
        ):
            _return_metrics(option)
            _reliability_details(option.reliability)
            _transit_details(option.transit_journey.legs)
            _bike_route_details(option.bike_route)
            _score_details(option.score)
            _alerts(option.matched_disruptions)
            if st.button(f"Confirm home #{rank}", key=f"confirm-return-{rank}"):
                try:
                    _confirm_return_selection(
                        plan,
                        rank,
                        _bike_state_store(config_path),
                    )
                except Exception as exc:
                    st.error(f"Confirmation stopped: {exc}")
                else:
                    st.session_state.pop("plan", None)
                    _flash_and_rerun("Return confirmed; bicycle recorded at home.")
    _rejections(plan.rejections)


def outbound_summary_rows(plan: OutboundPlan) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, option in enumerate(plan.options, start=1):
        route = option.bike_route
        rows.append(
            {
                "Rank": rank,
                "Type": (
                    "Bike + transit"
                    if option.kind is OutboundOptionKind.BIKE_TRANSIT
                    else "All transit"
                ),
                "Station": option.station.name if option.station else "—",
                "Bike": (
                    f"{route.duration_seconds / 60:.0f} min · {route.title}" if route else "—"
                ),
                "Transit": _transit_label(option.transit_journey.legs),
                "Walking": (f"{walking_duration_minutes(option.transit_journey):.0f} min"),
                "Arrival": option.arrival.strftime("%H:%M"),
                "Robust arrival": (
                    option.reliability.robust_arrival.strftime("%H:%M")
                    if option.reliability
                    else "—"
                ),
                "Confidence": (
                    option.reliability.confidence.value.title()
                    if option.reliability
                    else "Unknown"
                ),
                "Score": round(option.score.total_minutes, 1),
                "Alerts": len(option.matched_disruptions),
            }
        )
    return rows


def return_summary_rows(plan: ReturnPlan) -> list[dict[str, Any]]:
    return [
        {
            "Rank": rank,
            "Transit": _transit_label(option.transit_journey.legs),
            "Walking": f"{walking_duration_minutes(option.transit_journey):.0f} min",
            "Bike home": (
                f"{option.bike_route.duration_seconds / 60:.0f} min · {option.bike_route.title}"
            ),
            "Home": option.arrival.strftime("%H:%M"),
            "Robust home": (
                option.reliability.robust_arrival.strftime("%H:%M")
                if option.reliability
                else "—"
            ),
            "Confidence": (
                option.reliability.confidence.value.title()
                if option.reliability
                else "Unknown"
            ),
            "Score": round(option.score.total_minutes, 1),
            "Alerts": len(option.matched_disruptions),
        }
        for rank, option in enumerate(plan.options, start=1)
    ]


def _transit_label(legs: tuple[TransitLeg, ...]) -> str:
    labels: list[str] = []
    for leg in legs:
        if leg.type != "public_transport":
            continue
        mode = leg.commercial_mode or leg.mode or "Transit"
        codes = leg.equivalent_line_codes or ((leg.line_code,) if leg.line_code else ())
        label = f"{mode} {' / '.join(codes)}".strip()
        labels.append(label)
    return " → ".join(labels) or "No public-transport leg"


def _transit_details(legs: tuple[TransitLeg, ...]) -> None:
    rows = []
    for leg in legs:
        line = " / ".join(leg.equivalent_line_codes) or leg.line_code or ""
        rows.append(
            {
                "Time": _time_span(leg.departure, leg.arrival),
                "Mode": leg.commercial_mode or leg.mode or leg.type,
                "Line": line,
                "From": leg.origin_name or leg.origin_id or "",
                "To": leg.destination_name or leg.destination_id or "",
                "Duration": f"{leg.duration_seconds / 60:.0f} min",
                "Freshness": leg.freshness.value,
            }
        )
    st.markdown("**Transit legs**")
    st.dataframe(rows, hide_index=True, use_container_width=True)


def _option_metrics(option: OutboundOption) -> None:
    duration = (option.arrival - option.departure).total_seconds() / 60
    walking = walking_duration_minutes(option.transit_journey)
    longest = longest_walking_leg_minutes(option.transit_journey)
    columns = st.columns(4)
    columns[0].metric("Door to door", f"{duration:.0f} min")
    columns[1].metric("Arrival", option.arrival.strftime("%H:%M"))
    columns[2].metric("Walking", f"{walking:.0f} min")
    columns[3].metric("Longest walk", f"{longest:.0f} min")


def _return_metrics(option: ReturnOption) -> None:
    duration = (option.arrival - option.departure).total_seconds() / 60
    columns = st.columns(4)
    columns[0].metric("Door to door", f"{duration:.0f} min")
    columns[1].metric("Transit arrival", option.transit_journey.arrival.strftime("%H:%M"))
    columns[2].metric("Unlock bike", f"{option.retrieval_buffer_seconds / 60:g} min")
    columns[3].metric("Home", option.arrival.strftime("%H:%M"))


def _bike_route_details(route: Any) -> None:
    st.markdown("**Bicycle leg**")
    columns = st.columns(4)
    columns[0].metric("Route", route.title)
    columns[1].metric("Distance", f"{route.distance_m / 1000:.1f} km")
    columns[2].metric("Duration", f"{route.duration_seconds / 60:.0f} min")
    columns[3].metric("Elevation", f"+{route.vertical_gain_m:.0f} m")
    if route.distance_m > 0:
        st.caption(
            f"Recommended roads {route.recommended_roads_m / route.distance_m:.0%} · "
            f"discouraged {route.discouraged_roads_m / route.distance_m:.0%}"
        )


def _reliability_details(reliability: ReliabilityAssessment | None) -> None:
    if reliability is None:
        st.caption("Confidence unavailable")
        return
    age = (
        f"{reliability.data_age_seconds / 60:.1f} min"
        if reliability.data_age_seconds is not None
        else "unknown"
    )
    st.markdown(
        f"**Confidence: {reliability.confidence.value.title()}** · "
        f"provider data age {age} · "
        f"robust arrival **{reliability.robust_arrival:%H:%M}**"
    )
    if reliability.minimum_connection_margin_minutes is not None:
        st.markdown(
            f"Minimum usable connection margin: "
            f"**{reliability.minimum_connection_margin_minutes:.1f} min**"
        )
    for reason in reliability.reasons:
        st.caption(f"• {reason}")


def _score_details(score: Any) -> None:
    st.markdown(
        f"**Score {score.total_minutes:.1f}** · "
        f"door {score.door_to_door_minutes:.1f} · "
        f"bike {score.bike_penalty_minutes:.1f} · "
        f"transfers {score.transfer_penalty_minutes:.1f} · "
        f"alerts {score.disruption_penalty_minutes:.1f} · "
        f"freshness {score.freshness_penalty_minutes:.1f} · "
        f"connections {score.connection_margin_penalty_minutes:.1f} · "
        f"comfort {score.cycling_comfort_penalty_minutes:.1f}"
    )


def _alerts(disruptions: tuple[Any, ...]) -> None:
    for disruption in disruptions:
        st.warning(f"**{disruption.title}**\n\n{_clean_alert_message(disruption.message)}")


def _rejections(rejections: tuple[Any, ...]) -> None:
    if not rejections:
        return
    with st.expander(f"{len(rejections)} rejected route(s)"):
        for rejection in rejections:
            st.write(
                f"**{rejection.station_name or rejection.station_id}** · "
                f"{rejection.bike_route_title or 'route'} — {rejection.reason}"
            )


def _time_span(start: datetime | None, end: datetime | None) -> str:
    if start is None and end is None:
        return ""
    if start is None:
        return f"→ {end:%H:%M}"
    if end is None:
        return f"{start:%H:%M} →"
    return f"{start:%H:%M}-{end:%H:%M}"


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _as_time(value: time | datetime) -> time:
    return value.time() if isinstance(value, datetime) else value


def _flash_and_rerun(message: str) -> None:
    st.session_state["flash_message"] = message
    st.rerun()


def _style() -> None:
    st.markdown(
        """
        <style>
        .block-container {max-width: 1320px; padding-top: 2rem;}
        [data-testid="stMetric"] {
            border: 1px solid rgba(128, 128, 128, .22);
            border-radius: .7rem;
            padding: .75rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
