from __future__ import annotations

import numpy as np
import pandas as pd

from finrl.meta.data_processors.processor_yahoofinance import (
    YahooFinanceProcessor,
)
from finrl.meta.preprocessor.preprocessors import FeatureEngineer


def _make_feature_df() -> pd.DataFrame:
    """Synthetic daily data where an indicator has leading NaNs (warm-up).

    The first two rows have no indicator value yet; the future value 1.0 at
    2020-01-03 must NOT be back-filled into 2020-01-01 / 2020-01-02.
    """
    return pd.DataFrame(
        {
            "date": [
                "2020-01-01",
                "2020-01-02",
                "2020-01-03",
                "2020-01-04",
            ],
            "tic": ["A", "A", "A", "A"],
            "close": [100.0, 101.0, 102.0, 103.0],
            "my_indicator": [np.nan, np.nan, 1.0, 1.1],
        }
    )


def test_preprocess_data_does_not_backfill_future_indicator():
    """FeatureEngineer must not pull future indicator values into the past.

    bfill() was removed from preprocess_data() because it back-fills future
    observations (look-ahead bias). Leading NaNs (indicator warm-up) must
    remain missing rather than be fabricated from future data.
    """
    df = _make_feature_df()
    fe = FeatureEngineer(
        use_technical_indicator=False,
        use_vix=False,
        use_turbulence=False,
        user_defined_feature=False,
    )
    out = fe.preprocess_data(df)

    row_01_01 = out[out.date == "2020-01-01"].iloc[0]
    row_01_02 = out[out.date == "2020-01-02"].iloc[0]
    row_01_04 = out[out.date == "2020-01-04"].iloc[0]

    # Leading NaNs stay NaN: no future value (1.0) is back-filled into row 1/2.
    assert pd.isna(row_01_01["my_indicator"])
    assert pd.isna(row_01_02["my_indicator"])
    # ffill still carries a known value forward where a past value exists.
    assert row_01_04["my_indicator"] == 1.1


def test_df_to_array_does_not_backfill_future_price():
    """YahooProcessor.df_to_array must not pull a ticker's future price into
    earlier dates where that ticker has no observation yet."""
    df = pd.DataFrame(
        {
            "date": [
                "2020-01-01",
                "2020-01-02",
                "2020-01-03",
                "2020-01-03",
                "2020-01-04",
                "2020-01-04",
            ],
            "tic": ["B", "B", "A", "B", "A", "B"],
            "close": [10.0, 11.0, 20.0, 12.0, 21.0, 13.0],
            "turbulence": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        }
    )
    processor = YahooFinanceProcessor()
    price_array, _tech_array, _turbulence_array = processor.df_to_array(
        df, tech_indicator_list=[], if_vix=False
    )

    # stack() sorts tickers alphabetically -> ['A', 'B']; price_array is
    # (dates x tickers), so column 0 is A. A starts trading on 2020-01-03, so
    # its earliest rows (2020-01-01, 2020-01-02) must stay NaN: the future
    # price 20.0 at 2020-01-03 must NOT be back-filled into them.
    a_prices = price_array[:, 0].ravel()
    assert np.isnan(a_prices[0])
    assert np.isnan(a_prices[1])
    assert a_prices[2] == 20.0
    assert a_prices[3] == 21.0


def test_fundamental_ratio_ffill_does_not_use_future_report():
    """Quarterly fundamental ratios merged onto daily rows must not be
    back-filled with the next (future, not-yet-reported) ratio.

    ffill(axis="rows") carries the last *reported* ratio forward (correct,
    you know the last reported ratio). bfill(axis="rows") would pull the next
    quarter's ratio back into earlier daily rows before it was reported
    (look-ahead bias).
    """
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"]
            ),
            "tic": ["A", "A", "A", "A"],
            "close": [100.0, 101.0, 102.0, 103.0],
            "EPS": [np.nan, np.nan, 2.0, np.nan],  # Q1 report lands 2020-01-03
        }
    )
    out = df.sort_values(["tic", "date"]).ffill(axis="rows")

    # Before the first report, EPS is genuinely unknown: stay NaN (honest),
    # do not fabricate it from the future 2.0 report.
    assert pd.isna(out[out.date == "2020-01-01"].iloc[0]["EPS"])
    assert pd.isna(out[out.date == "2020-01-02"].iloc[0]["EPS"])
    # On/after the report date, the last reported ratio carries forward.
    assert out[out.date == "2020-01-03"].iloc[0]["EPS"] == 2.0
    assert out[out.date == "2020-01-04"].iloc[0]["EPS"] == 2.0
