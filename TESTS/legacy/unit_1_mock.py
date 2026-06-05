# File: unit_1_flux_patch.py

import sys
import os
import unittest
import logging
import numpy as np
import polars as pl

from datetime import datetime, timedelta
from unittest.mock import patch

import flux
from flux import Timeline

# Ensure we can import fluxstate from its parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxstate import FluxState

try:
    from snowflake.snowpark import Session
except ImportError:
    Session = None


class TestFluxStateWithPatchedDatetime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """
        Runs once before any tests. Setup logging, optional Snowflake session, test data, etc.
        """
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler(), logging.FileHandler("test_flux_patch.log")],
        )
        logging.info("Setting up test environment for FluxState with a specialized datetime patch.")

        cls.table_name = "TEST_MAIN_TABLE"
        cls.mirror_table_name = "TEST_MIRROR_TABLE"
        cls.final_mirror_parquet_path = "final_mirror_table.parquet"
        cls.debug_csv_path = "debug.csv"

        # Create a Snowflake session if available
        if Session is not None:
                    connection_parameters = {
            "account": os.environ.get("SNOWFLAKE_ACCOUNT"),
            "user": os.environ.get("SNOWFLAKE_USER"),
            "password": os.environ.get("SNOWFLAKE_PASSWORD"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "HERSELF_DEFAULT_XSMALL"),
            "database": os.environ.get("SNOWFLAKE_DATABASE", "TWICE"),
        }
            cls.session = Session.builder.configs(connection_parameters).create()
            logging.info("Snowflake session created successfully.")
        else:
            cls.session = None
            logging.warning("Snowflake session not available.")

        # Create initial Polars DataFrame
        cls.num_rows, cls.num_columns = 5, 3
        cls.initial_data = np.random.randint(0, 100, size=(cls.num_rows, cls.num_columns), dtype=np.int8)
        cls.column_names = [f"COLUMN_{i}" for i in range(cls.num_columns)]
        cls.key_column_name = "COLUMN_0"

        df = pl.DataFrame(cls.initial_data, schema=cls.column_names)
        # Make COLUMN_0 unique
        df = df.with_columns([
            pl.Series(name=cls.key_column_name, values=range(1, cls.num_rows + 1)).cast(pl.Int8)
        ])
        cls.initial_df = df

        if cls.session:
            cls.session.write_pandas(df.to_pandas(), cls.table_name, auto_create_table=True, overwrite=True)
            logging.info("Initial data saved to Snowflake.")

    def setUp(self):
        """
        Runs before each test. Create a fresh timeline and freeze it (time factor=0).
        """
        self.timeline = Timeline()
        self.timeline.set_time_factor(0)
        logging.info("Flux timeline created and frozen (time factor=0).")

        # Optionally set an initial offset if desired:
        # self.timeline.sleep(100)  # e.g. start 100s in the future

    def test_flux_state_with_flux_time(self):
        """
        Demonstrates a specialized patch of `fluxstate.datetime` so that it calls Flux's timeline.
        """
        # -------------------------------------------------------------------------------------
        # 1) Define a custom datetime subclass that delegates .now() to our flux timeline
        # -------------------------------------------------------------------------------------
        class PatchedDateTime(datetime):
            """
            A subclass of built-in `datetime.datetime` that overrides `now()` to use Flux.
            This preserves isinstance(value, datetime), so it won't break fluxstate checks.
            """
            @classmethod
            def now(cls, tz=None):
                # We'll interpret flux.current_timeline.time() as seconds since some anchor.
                offset_seconds = self.timeline.time()
                anchor = datetime(2023, 1, 1)  # pick an arbitrary anchor date
                actual = anchor + timedelta(seconds=offset_seconds)
                if tz:
                    return tz.fromutc(actual)
                return actual

        # 2) Load your Polars DataFrame from Snowflake or from initial_df
        if self.session:
            pdf = self.session.table(self.table_name).to_pandas()
            table_df = pl.DataFrame(pdf)
        else:
            table_df = self.initial_df.clone()

        # Log initial data structure
        logging.info("\nINITIAL DATA STRUCTURE:")
        for col in table_df.columns:
            sample_value = table_df[col][0]
            logging.info(f"Column {col} first value: {sample_value} (type: {type(sample_value)})")

        patch_path = "fluxstate.datetime"

        with patch(patch_path, PatchedDateTime):
            # 4) Now inside this context, fluxstate's `datetime.now()` calls use our PatchedDateTime
            flux_state = FluxState(table=table_df, key_column=self.key_column_name, mode="init")
            
            # Log structure right after initialization
            logging.info("\nAFTER INITIALIZATION:")
            for col in flux_state.mirror_table:
                if col != flux_state.key_column:
                    for i, rowdata in enumerate(flux_state.mirror_table[col]):
                        logging.info(f"Col={col}, Row={i}: {rowdata} (type: {type(rowdata)})")
                        if isinstance(rowdata, list):
                            logging.info(f"  -> First item type: {type(rowdata[0])}")

            # 5) First update
            flux_state.update_mirror_table()
            
            # Log structure after first update
            logging.info("\nAFTER FIRST UPDATE:")
            for col in flux_state.mirror_table:
                if col != flux_state.key_column:
                    for i, rowdata in enumerate(flux_state.mirror_table[col]):
                        logging.info(f"Col={col}, Row={i}: {rowdata} (type: {type(rowdata)})")
                        if isinstance(rowdata, list):
                            logging.info(f"  -> First item type: {type(rowdata[0])}")

            # 6) Advance timeline
            self.timeline.sleep(2 * 24 * 3600)

            # 7) Second update
            flux_state.update_mirror_table()
            
            # Log structure after second update
            logging.info("\nAFTER SECOND UPDATE:")
            for col in flux_state.mirror_table:
                if col != flux_state.key_column:
                    for i, rowdata in enumerate(flux_state.mirror_table[col]):
                        logging.info(f"Col={col}, Row={i}: {rowdata} (type: {type(rowdata)})")
                        if isinstance(rowdata, list):
                            logging.info(f"  -> First item type: {type(rowdata[0])}")

            # 8) Before save
            logging.info("\nBEFORE SAVE_MIRROR_TABLE:")
            for col in flux_state.mirror_table:
                if col != flux_state.key_column:
                    for i, rowdata in enumerate(flux_state.mirror_table[col]):
                        logging.info(f"Col={col}, Row={i}: {rowdata} (type: {type(rowdata)})")
                        if isinstance(rowdata, list):
                            logging.info(f"  -> First item type: {type(rowdata[0])}")
                            if isinstance(rowdata[0], list):
                                logging.info(f"    -> DOUBLE NESTING DETECTED in {col}, Row {i}")

            # Save mirror table
            flux_state.save_mirror_table(
                output_format="both",
                output_path_parquet="test_mirror_table.parquet",
                output_path_csv="test_mirror_table.csv"
            )

        # Outside the `with patch(...)` block, real datetime is restored
        self.assertTrue(os.path.exists("test_mirror_table.parquet"))
        self.assertTrue(os.path.exists("test_mirror_table.csv"))
        logging.info("Test flux_state_with_flux_time completed successfully.")

    @classmethod
    def tearDownClass(cls):
        """
        Runs once after all tests. Cleanup Snowflake tables and local files if needed.
        """
        if cls.session:
            logging.info("Cleaning up Snowflake tables.")
            cls.session.sql(f"DROP TABLE IF EXISTS {cls.table_name}").collect()
            cls.session.sql(f"DROP TABLE IF EXISTS {cls.mirror_table_name}").collect()
            cls.session.close()

        for path in ["test_mirror_table.parquet", "test_mirror_table.csv"]:
            if os.path.exists(path):
                os.remove(path)
        logging.info("All test artifacts removed.")


if __name__ == "__main__":
    unittest.main()
