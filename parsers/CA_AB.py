#!/usr/bin/env python3

"""Parse the Alberta Electric System Operator's (AESO's) Energy Trading System
(ETS) website.
"""

# Standard library imports
import csv
import io
import re
import urllib.parse
from datetime import datetime, timedelta
from logging import Logger, getLogger
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from bs4 import BeautifulSoup

# Third-party library imports
from requests import Session

from electricitymap.contrib.lib.models.event_lists import (
    ExchangeList,
    GridAlertList,
    PriceList,
    ProductionBreakdownList,
)
from electricitymap.contrib.lib.models.events import (
    EventSourceType,
    GridAlertType,
    ProductionMix,
)
from electricitymap.contrib.lib.types import ZoneKey

# Local library imports
from parsers.lib import validation

DEFAULT_ZONE_KEY = ZoneKey("CA-AB")
MINIMUM_PRODUCTION_THRESHOLD = 10  # MW
TIMEZONE = ZoneInfo("Canada/Mountain")
URL = urllib.parse.urlsplit("http://ets.aeso.ca/ets_web/ip/Market/Reports")
URL_STRING = urllib.parse.urlunsplit(URL)
GRID_ALERTS_URL = "http://ets.aeso.ca/ets_web/ip/Market/Reports/RealTimeShiftReportServlet?contentType=html"
GRID_ALERT_SOURCE = "aeso.ca"


def fetch_exchange(
    zone_key1: str = DEFAULT_ZONE_KEY,
    zone_key2: str = "CA-BC",
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict[str, Any]]:
    """Request the last known power exchange (in MW) between two countries."""
    if target_datetime:
        raise NotImplementedError("Currently unable to scrape historical data")
    session = session or Session()
    response = session.get(
        f"{URL_STRING}/CSDReportServlet", params={"contentType": "csv"}
    )
    interchange = dict(csv.reader(response.text.split("\r\n\r\n")[4].splitlines()))
    flows = {
        f"{DEFAULT_ZONE_KEY}->CA-BC": interchange["British Columbia"],
        f"{DEFAULT_ZONE_KEY}->CA-SK": interchange["Saskatchewan"],
        f"{DEFAULT_ZONE_KEY}->US-MT": interchange["Montana"],
        f"{DEFAULT_ZONE_KEY}->US-NW-NWMT": interchange["Montana"],
    }
    sorted_zone_keys = ZoneKey("->".join(sorted((zone_key1, zone_key2))))
    if sorted_zone_keys not in flows:
        raise NotImplementedError(f"Pair '{sorted_zone_keys}' not implemented")
    exchanges = ExchangeList(logger)
    exchanges.append(
        zoneKey=sorted_zone_keys,
        datetime=get_csd_report_timestamp(response.text),
        netFlow=float(flows[sorted_zone_keys]),
        source=URL.netloc,
    )
    return exchanges.to_list()


def fetch_price(
    zone_key: ZoneKey = DEFAULT_ZONE_KEY,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict[str, Any]]:
    """Request the last known power price of a given country."""
    if target_datetime:
        raise NotImplementedError("Currently unable to scrape historical data")
    session = session or Session()
    response = session.get(
        f"{URL_STRING}/SMPriceReportServlet", params={"contentType": "csv"}
    )
    prices = PriceList(logger)
    for row in csv.reader(response.text.split("\r\n\r\n")[2].splitlines()[1:]):
        if row[1] != "-":
            date, hour = row[0].split()
            prices.append(
                zoneKey=zone_key,
                datetime=datetime.strptime(
                    f"{date} {int(hour) - 1}", "%m/%d/%Y %H"
                ).replace(tzinfo=TIMEZONE)
                + timedelta(hours=1),
                price=float(row[1]),
                source=URL.netloc,
                currency="CAD",
            )
    return prices.to_list()


def fetch_production(
    zone_key: str = DEFAULT_ZONE_KEY,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> dict[str, Any] | None:
    """Request the last known production mix (in MW) of a given country."""
    if target_datetime:
        raise NotImplementedError("This parser is not yet able to parse past dates")
    session = session or Session()
    response = session.get(
        f"{URL_STRING}/CSDReportServlet", params={"contentType": "csv"}
    )
    generation = {
        row[0]: {
            "MC": float(row[1]),  # maximum capability
            "TNG": float(row[2]),  # total net generation
        }
        for row in csv.reader(response.text.split("\r\n\r\n")[3].splitlines())
    }
    return [
        validation.validate(
            {
                "capacity": {
                    "gas": generation["COGENERATION"]["MC"]
                    + generation["COMBINED CYCLE"]["MC"]
                    + generation["GAS FIRED STEAM"]["MC"]
                    + generation["SIMPLE CYCLE"]["MC"],
                    "wind": generation["WIND"]["MC"],
                    "solar": generation["SOLAR"]["MC"],
                    "hydro": generation["HYDRO"]["MC"],
                    "biomass": generation["OTHER"]["MC"],
                    "battery storage": generation["ENERGY STORAGE"]["MC"],
                },
                "datetime": get_csd_report_timestamp(response.text),
                "production": {
                    "gas": generation["COGENERATION"]["TNG"]
                    + generation["COMBINED CYCLE"]["TNG"]
                    + generation["GAS FIRED STEAM"]["TNG"]
                    + generation["SIMPLE CYCLE"]["TNG"],
                    "wind": generation["WIND"]["TNG"],
                    "solar": generation["SOLAR"]["TNG"],
                    "hydro": generation["HYDRO"]["TNG"],
                    "biomass": generation["OTHER"]["TNG"],
                },
                "source": URL.netloc,
                "storage": {
                    "battery": generation["ENERGY STORAGE"]["TNG"],
                },
                "zoneKey": zone_key,
            },
            logger,
            floor=MINIMUM_PRODUCTION_THRESHOLD,
            remove_negative=True,
        )
    ]


def get_csd_report_timestamp(report):
    """Get the timestamp from a current supply/demand (CSD) report."""
    return datetime.strptime(
        re.search(r'"Last Update : (.*)"', report).group(1), "%b %d, %Y %H:%M"
    ).replace(tzinfo=TIMEZONE)


def _get_wind_solar_data(session: Session, url: str) -> pd.DataFrame:
    response = session.get(url)
    csv = pd.read_csv(io.StringIO(response.text))
    return csv


def fetch_wind_solar_forecasts(
    zone_key: ZoneKey = DEFAULT_ZONE_KEY,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict[str, Any]]:
    """Requests wind and solar production forecasts in hourly data (in MW) for 7 days ahead."""
    session = session or Session()

    # Requests
    # Wind 7 days
    url_wind = "http://ets.aeso.ca/Market/Reports/Manual/Operations/prodweb_reports/wind_solar_forecast/wind_rpt_longterm.csv"
    csv_wind = _get_wind_solar_data(session, url_wind)

    # Solar 7 days
    url_solar = "http://ets.aeso.ca/Market/Reports/Manual/Operations/prodweb_reports/wind_solar_forecast/solar_rpt_longterm.csv"
    csv_solar = _get_wind_solar_data(session, url_solar)

    all_production_events = csv_wind.merge(
        csv_solar, on="Forecast Transaction Date", suffixes=("_wind", "_solar")
    )

    production_list = ProductionBreakdownList(logger)
    for _, event in all_production_events.iterrows():
        event_datetime = event["Forecast Transaction Date"]
        event_datetime = datetime.fromisoformat(event_datetime).replace(tzinfo=TIMEZONE)
        production_mix = ProductionMix()
        production_mix.add_value(
            "solar", event["Most Likely_solar"], correct_negative_with_zero=True
        )
        production_mix.add_value(
            "wind", event["Most Likely_wind"], correct_negative_with_zero=True
        )

        production_list.append(
            zoneKey=zone_key,
            datetime=event_datetime,
            production=production_mix,
            source=URL.netloc,
            sourceType=EventSourceType.forecasted,
        )
    return production_list.to_list()


def fetch_grid_alerts(
    zone_key: ZoneKey = DEFAULT_ZONE_KEY,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict[str, Any]]:
    session = session or Session()

    data = session.get(GRID_ALERTS_URL)
    soup = BeautifulSoup(data.text, "html.parser")
    table = soup.find_all("table")[-1]
    rows = table.find_all("tr")
    grid_alert_list = GridAlertList(logger)
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        message = cells[1].text
        time = cells[0].text
        time = datetime.strptime(time, "%m/%d/%Y %H:%M").replace(tzinfo=TIMEZONE)
        grid_alert_list.append(
            zoneKey=zone_key,
            locationRegion=None,
            source=GRID_ALERT_SOURCE,
            alertType=GridAlertType.undefined,
            message=message,
            issuedTime=time,
            startTime=None,
            endTime=None,
        )
    return grid_alert_list.to_list()


if __name__ == "__main__":
    """Main method, never used by the electricityMap backend, but handy for testing."""
    """
    print("fetch_production() ->")
    print(fetch_production())
    print("fetch_price() ->")
    print(fetch_price())
    print(f"fetch_exchange({DEFAULT_ZONE_KEY}, CA-BC) ->")
    print(fetch_exchange(DEFAULT_ZONE_KEY, "CA-BC"))
    print(f"fetch_exchange({DEFAULT_ZONE_KEY}, CA-SK) ->")
    print(fetch_exchange(DEFAULT_ZONE_KEY, "CA-SK"))
    print(f"fetch_exchange({DEFAULT_ZONE_KEY}, US-MT) ->")
    print(fetch_exchange(DEFAULT_ZONE_KEY, "US-MT"))
    print(f"fetch_exchange({DEFAULT_ZONE_KEY}, US-NW-NWMT) ->")
    print(fetch_exchange(DEFAULT_ZONE_KEY, "US-NW-NWMT"))"
    """
    print(fetch_wind_solar_forecasts())
