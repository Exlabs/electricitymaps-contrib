from datetime import datetime, timedelta
from logging import Logger, getLogger
from operator import itemgetter
from zoneinfo import ZoneInfo

from requests import Response, Session

from electricitymap.contrib.lib.models.event_lists import (
    ExchangeList,
    ProductionBreakdownList,
    TotalConsumptionList,
    TotalProductionList,
)
from electricitymap.contrib.lib.models.events import EventSourceType, ProductionMix
from electricitymap.contrib.lib.types import ZoneKey
from parsers.lib.config import refetch_frequency
from parsers.lib.exceptions import ParserException

IE_TZ = ZoneInfo("Europe/Dublin")
URL = "https://www.smartgriddashboard.com/api/chart"

SOURCE = "eirgridgroup.com"

KINDS_AREA_MAPPING = {
    "demand": "demandactual",
    "demand_forecast": "demandforecast",
    "wind": "windactual",
    "wind_forecast": "windforecast",
    "exchange": "interconnection",
    "generation": "generationactual",
    "solar": "solaractual",
    "solar_forecast": "solarforecast",
}

REGION_MAPPING = {
    "IE": "ROI",
    "GB-NIR": "NI",
}

EXCHANGE_MAPPING = {
    ZoneKey("GB->IE"): {
        "key": "ROI",
        "exchange": ["INTER_EWIC", "INTER_GRNLK"],
        "direction": 1,
    },
    ZoneKey("GB->GB-NIR"): {"key": "NI", "exchange": ["INTER_MOYLE"], "direction": 1},
}


def get_datetime_params(datetime: datetime) -> dict:
    return {
        "datefrom": (datetime - timedelta(days=2)).strftime("%Y-%m-%d"),
        "dateto": (datetime + timedelta(days=1)).strftime("%Y-%m-%d"),
    }


def parse_datetime(datetime_str: str) -> datetime:
    return datetime.strptime(datetime_str, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IE_TZ)


def fetch_data(
    target_datetime: datetime,
    zone_key: ZoneKey,
    kind: str,
    session: Session,
) -> list:
    """
    Gets values and corresponding datetimes for the specified data kind in ROI.
    Removes any values that are in the future or don't have a datetime associated with them.
    """
    assert isinstance(target_datetime, datetime)
    assert kind != ""
    assert session is not None

    resp: Response = session.get(
        url=URL,
        params={
            "areas": KINDS_AREA_MAPPING[kind],
            "chartType": "default",
            "region": EXCHANGE_MAPPING[zone_key]["key"]
            if zone_key in EXCHANGE_MAPPING
            else REGION_MAPPING[zone_key],
            "dateRange": "day",
            **get_datetime_params(target_datetime),
        },
    )
    try:
        data = resp.json().get("Rows", [])
    except Exception as e:
        raise ParserException(
            parser="SMARTGRIDDASHBOARD.py",
            message=f"{target_datetime}: {kind} data is not available for {zone_key}",
        ) from e
    return data


def parse_consumption(
    zone_key: ZoneKey,
    session: Session | None,
    target_datetime: datetime | None,
    logger: Logger,
    forecast: bool,
) -> list:
    """gets forecasted consumption values for ROI"""

    session = session or Session()

    if target_datetime is None:
        target_datetime = datetime.now(tz=IE_TZ)

    data = fetch_data(
        target_datetime=target_datetime,
        zone_key=zone_key,
        kind="demand_forecast" if forecast else "demand",
        session=session,
    )

    demandList = TotalConsumptionList(logger=logger)
    for item in data:
        dt = parse_datetime(item["EffectiveTime"])
        # when fetching real-time data we remove future values
        if not forecast:
            now = datetime.now(tz=IE_TZ)
            # datetimes in the future are expected to be None
            if dt > now:
                continue

        demandList.append(
            zoneKey=zone_key,
            consumption=item["Value"],
            datetime=dt,
            source=SOURCE,
            sourceType=EventSourceType.forecasted
            if forecast
            else EventSourceType.measured,
        )
    return demandList.to_list()


@refetch_frequency(timedelta(days=1))
def fetch_production(
    zone_key: ZoneKey,
    session: Session = Session(),
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list:
    """Gets values for wind production and estimates unknwon production as demand - wind - exchange"""
    if target_datetime is None:
        target_datetime = datetime.now(tz=IE_TZ)

    total_generation = fetch_data(
        target_datetime=target_datetime,
        zone_key=zone_key,
        kind="generation",
        session=session,
    )

    wind_data = fetch_data(
        target_datetime=target_datetime, zone_key=zone_key, kind="wind", session=session
    )

    solar_data = fetch_data(
        target_datetime=target_datetime,
        zone_key=zone_key,
        kind="solar",
        session=session,
    )

    assert len(total_generation) > 0
    assert len(wind_data) > 0
    assert len(solar_data) > 0

    # sort by time
    total_generation.sort(key=itemgetter("EffectiveTime"))
    wind_data.sort(key=itemgetter("EffectiveTime"))
    solar_data.sort(key=itemgetter("EffectiveTime"))

    production = ProductionBreakdownList(logger=logger)

    for total, wind, solar in zip(total_generation, wind_data, solar_data, strict=True):
        dt = parse_datetime(total["EffectiveTime"])
        dt_wind = parse_datetime(wind["EffectiveTime"])
        dt_solar = parse_datetime(solar["EffectiveTime"])

        assert dt == dt_wind == dt_solar

        now = datetime.now(tz=IE_TZ)
        # datetimes in the future are expected to be None
        if dt > now:
            continue

        total_prod = total.get("Value")
        wind_prod = wind.get("Value")
        solar_prod = solar.get("Value")

        productionMix = ProductionMix()
        if all([total_prod is not None, wind_prod is not None, solar_prod is not None]):
            productionMix.add_value(
                "unknown",
                total_prod - wind_prod - solar_prod,
            )
            productionMix.add_value("wind", wind_prod, correct_negative_with_zero=True)
            productionMix.add_value(
                "solar", solar_prod, correct_negative_with_zero=True
            )

        production.append(
            zoneKey=zone_key,
            production=productionMix,
            datetime=dt,
            source=SOURCE,
        )

    return production.to_list()


@refetch_frequency(timedelta(days=1))
def fetch_exchange(
    zone_key1: ZoneKey,
    zone_key2: ZoneKey,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list:
    """
    Fetches exchanges values for the East-West (GB->IE) and Moyle (GB->GB-NIR)
    interconnectors.
    """
    session = session or Session()

    if target_datetime is None:
        target_datetime = datetime.now(tz=IE_TZ)

    exchangeKey = ZoneKey("->".join(sorted([zone_key1, zone_key2])))

    if exchangeKey == "GB-NIR->IE":
        raise ParserException(
            parser="SMARTGRIDDASHBOARD.py",
            message="the GB-NIR_IE interconnection is unsupported.",
        )

    exchange_data = fetch_data(
        target_datetime=target_datetime,
        zone_key=exchangeKey,
        kind="exchange",
        session=session,
    )

    exchange_mapping = EXCHANGE_MAPPING[exchangeKey]
    exchanges = {x: ExchangeList(logger=logger) for x in exchange_mapping["exchange"]}

    for exchange in exchange_data:
        target_exchange = exchanges.get(exchange["FieldName"])
        if target_exchange is None:
            continue

        dt = parse_datetime(exchange["EffectiveTime"])
        now = datetime.now(tz=IE_TZ)
        # datetimes in the future are expected to be None
        if dt > now:
            continue

        flow = (
            exchange["Value"] * exchange_mapping["direction"]
            if exchange["Value"]
            else exchange["Value"]
        )

        target_exchange.append(
            zoneKey=exchangeKey,
            netFlow=flow,
            datetime=parse_datetime(exchange["EffectiveTime"]),
            source=SOURCE,
        )

    return ExchangeList.merge_exchanges(exchanges.values(), logger=logger).to_list()


@refetch_frequency(timedelta(days=1))
def fetch_consumption(
    zone_key: ZoneKey,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list:
    """gets consumption values for ROI"""
    return parse_consumption(
        zone_key=zone_key,
        session=session,
        target_datetime=target_datetime,
        logger=logger,
        forecast=False,
    )


@refetch_frequency(timedelta(days=1))
def fetch_consumption_forecast(
    zone_key: ZoneKey,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list:
    """gets forecasted consumption values for ROI"""
    return parse_consumption(
        zone_key=zone_key,
        session=session,
        target_datetime=target_datetime,
        logger=logger,
        forecast=True,
    )


@refetch_frequency(timedelta(days=1))
def fetch_wind_solar_forecasts(
    zone_key: ZoneKey,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list:
    """
    Gets values and corresponding datetimes for forecasted wind produciton.
    """

    session = session or Session()

    if target_datetime is None:
        target_datetime = datetime.now(tz=IE_TZ)

    wind_forecast_data = fetch_data(
        target_datetime=target_datetime,
        zone_key=zone_key,
        kind="wind_forecast",
        session=session,
    )

    wind_forecast = ProductionBreakdownList(logger=logger)
    for item in wind_forecast_data:
        productionMix = ProductionMix()
        productionMix.add_value("wind", item["Value"], correct_negative_with_zero=True)
        wind_forecast.append(
            zoneKey=zone_key,
            production=productionMix,
            datetime=parse_datetime(item["EffectiveTime"]),
            source=SOURCE,
            sourceType=EventSourceType.forecasted,
        )

    solar_forecast_data = fetch_data(
        target_datetime=target_datetime,
        zone_key=zone_key,
        kind="solar_forecast",
        session=session,
    )

    solar_forecast = ProductionBreakdownList(logger=logger)
    for item in solar_forecast_data:
        productionMix = ProductionMix()
        productionMix.add_value("solar", item["Value"], correct_negative_with_zero=True)
        solar_forecast.append(
            zoneKey=zone_key,
            production=productionMix,
            datetime=parse_datetime(item["EffectiveTime"]),
            source=SOURCE,
            sourceType=EventSourceType.forecasted,
        )

    return ProductionBreakdownList.merge_production_breakdowns(
        [wind_forecast, solar_forecast], logger=logger
    ).to_list()


def fetch_total_generation(
    zone_key: ZoneKey,
    session: Session | None = None,
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list:
    """
    Gets values and corresponding datetimes for the total generation.
    This is the sum of all generation reported as a single value.
    """

    session = session or Session()

    if target_datetime is None:
        target_datetime = datetime.now(tz=IE_TZ)

    generation_data = fetch_data(
        target_datetime=target_datetime,
        zone_key=zone_key,
        kind="generation",
        session=session,
    )
    total_generation = TotalProductionList(logger=logger)
    for item in generation_data:
        total_generation.append(
            zoneKey=zone_key,
            value=item["Value"],
            datetime=parse_datetime(item["EffectiveTime"]),
            source=SOURCE,
            sourceType=EventSourceType.measured,
        )
    return total_generation.to_list()
