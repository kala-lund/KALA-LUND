from __future__ import annotations

import unittest
from unittest.mock import create_autospec, patch

import pandas as pd
from pandas.testing import assert_frame_equal

from finrl.meta.preprocessor.yahoodownloader import YahooDownloader


class TestYahooDownloaderAdjustPrices(unittest.TestCase):

    def setUp(self):
        """Set up a dummy YahooDownloader instance and test data."""
        # These init params are not used by _adjust_prices but needed for instantiation
        self.downloader = YahooDownloader(
            start_date="2020-01-01",
            end_date="2020-01-03",
            ticker_list=["AAPL"],
        )
        # Create a sample DataFrame similar to what fetch_data might produce before adjustment
        self.raw_data = pd.DataFrame(
            {
                "date": ["2020-01-01", "2020-01-02"],
                "open": [100.0, 102.0],
                "high": [105.0, 106.0],
                "low": [98.0, 100.0],
                "close": [104.0, 105.0],
                "adjcp": [
                    100.0,
                    102.9,
                ],  # Adjusted close price - crucial for the method
                "volume": [10000, 11000],
                "tic": ["AAPL", "AAPL"],
            }
        )

    def test_adjust_prices_calculates_correctly(self):
        """Test that prices are adjusted correctly based on adjcp/close ratio."""
        # Explicitly ensure columns exist before passing to the method
        self.assertIn("adjcp", self.raw_data.columns)
        self.assertIn("close", self.raw_data.columns)

        adjusted_df = self.downloader._adjust_prices(self.raw_data.copy())

        # Calculate expected values
        adj_ratio_1 = self.raw_data.loc[0, "adjcp"] / self.raw_data.loc[0, "close"]
        adj_ratio_2 = self.raw_data.loc[1, "adjcp"] / self.raw_data.loc[1, "close"]

        expected_data = pd.DataFrame(
            {
                "date": ["2020-01-01", "2020-01-02"],
                "open": [
                    self.raw_data.loc[0, "open"] * adj_ratio_1,
                    self.raw_data.loc[1, "open"] * adj_ratio_2,
                ],
                "high": [
                    self.raw_data.loc[0, "high"] * adj_ratio_1,
                    self.raw_data.loc[1, "high"] * adj_ratio_2,
                ],
                "low": [
                    self.raw_data.loc[0, "low"] * adj_ratio_1,
                    self.raw_data.loc[1, "low"] * adj_ratio_2,
                ],
                "close": [
                    self.raw_data.loc[0, "adjcp"],
                    self.raw_data.loc[1, "adjcp"],
                ],  # close becomes adjcp
                "volume": [10000, 11000],
                "tic": ["AAPL", "AAPL"],
            }
        )

        # Select only the columns present in the expected output for comparison
        # and ensure the same column order and index
        adjusted_df_compare = adjusted_df[expected_data.columns].reset_index(drop=True)
        expected_data = expected_data.reset_index(drop=True)

        # Use pandas testing utility for robust DataFrame comparison
        assert_frame_equal(adjusted_df_compare, expected_data, check_dtype=True)

    def test_adjust_prices_drops_columns(self):
        """Test that 'adjcp' and the temporary 'adj' columns are dropped."""
        # Explicitly ensure columns exist before passing to the method
        self.assertIn("adjcp", self.raw_data.columns)
        self.assertIn("close", self.raw_data.columns)

        adjusted_df = self.downloader._adjust_prices(self.raw_data.copy())

        self.assertNotIn("adjcp", adjusted_df.columns)
        self.assertNotIn("adj", adjusted_df.columns)
        # Ensure other essential columns remain
        self.assertIn("open", adjusted_df.columns)
        self.assertIn(
            "close", adjusted_df.columns
        )  # Note: This is the *new* adjusted close
        self.assertIn("tic", adjusted_df.columns)
        self.assertIn("date", adjusted_df.columns)
        self.assertIn("volume", adjusted_df.columns)


def _make_multiindex_download_df() -> pd.DataFrame:
    """Return a yfinance-style DataFrame with MultiIndex columns.

    Exercises the `droplevel(1)` branch in `YahooDownloader.fetch_data`.
    """
    idx = pd.DatetimeIndex(
        ["2020-01-01", "2020-01-02", "2020-01-03"], name="Date"
    )
    cols = pd.MultiIndex.from_tuples(
        [
            ("Open", "AAPL"),
            ("High", "AAPL"),
            ("Low", "AAPL"),
            ("Close", "AAPL"),
            ("Adj Close", "AAPL"),
            ("Volume", "AAPL"),
        ]
    )
    data = {
        ("Open", "AAPL"): [100.0, 102.0, 101.0],
        ("High", "AAPL"): [105.0, 106.0, 104.0],
        ("Low", "AAPL"): [98.0, 100.0, 99.0],
        ("Close", "AAPL"): [104.0, 105.0, 103.0],
        ("Adj Close", "AAPL"): [100.0, 102.9, 101.5],
        ("Volume", "AAPL"): [10000, 11000, 10500],
    }
    return pd.DataFrame(data, index=idx, columns=cols)


def _download_with_proxy(tickers, start=None, end=None, proxy=None, auto_adjust=False):
    """yfinance < 1.7.0 style `download` that accepts `proxy`."""
    return _make_multiindex_download_df()


def _download_without_proxy(tickers, start=None, end=None, auto_adjust=False):
    """yfinance >= 1.7.0 style `download` that rejects `proxy`."""
    return _make_multiindex_download_df()


class TestYahooDownloaderProxyCompatibility(unittest.TestCase):
    """Regression test for the yfinance `proxy` keyword incompatibility."""

    def setUp(self):
        self.downloader = YahooDownloader(
            start_date="2020-01-01",
            end_date="2020-01-03",
            ticker_list=["AAPL"],
        )

    def test_fetch_data_forwards_proxy_when_supported(self):
        """`proxy` is forwarded and no TypeError is raised when supported."""
        with patch(
            "finrl.meta.preprocessor.yahoodownloader.yf.download",
            create_autospec(_download_with_proxy),
        ) as mock_download:
            mock_download.return_value = _make_multiindex_download_df()
            df = self.downloader.fetch_data(
                proxy="http://proxy:8080", auto_adjust=False
            )
            self.assertIsInstance(df, pd.DataFrame)
            self.assertFalse(df.empty)
            mock_download.assert_called()
            self.assertEqual(
                mock_download.call_args.kwargs.get("proxy"), "http://proxy:8080"
            )

    def test_fetch_data_no_typeerror_when_proxy_unsupported(self):
        """No TypeError when `proxy` is rejected (yfinance >= 1.7.0)."""
        with patch(
            "finrl.meta.preprocessor.yahoodownloader.yf.download",
            create_autospec(_download_without_proxy),
        ) as mock_download:
            mock_download.return_value = _make_multiindex_download_df()
            df = self.downloader.fetch_data(
                proxy="http://proxy:8080", auto_adjust=False
            )
            self.assertIsInstance(df, pd.DataFrame)
            self.assertFalse(df.empty)
            mock_download.assert_called()
            self.assertNotIn("proxy", mock_download.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
