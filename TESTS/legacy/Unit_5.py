import unittest
import numpy as np
import polars as pl
from datetime import datetime, timedelta
from time import sleep
import logging
import json
import os
from snowflake.snowpark import Session
from unittest.mock import patch
import sys
# Add the parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxstate import FluxState
import pandas as pd


class TestFluxStateView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("test_flux_state_view.log"),
            ],
        )

        logging.info("Setting up test environment for FluxState with view.")

        # Create a Snowpark session
                connection_parameters = {
            "account": os.environ.get("SNOWFLAKE_ACCOUNT"),
            "user": os.environ.get("SNOWFLAKE_USER"),
            "password": os.environ.get("SNOWFLAKE_PASSWORD"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "HERSELF_DEFAULT_XSMALL"),
            "database": os.environ.get("SNOWFLAKE_DATABASE", "TWICE"),
        }

        try:
            logging.info("Creating Snowflake session...")
            cls.session = Session.builder.configs(connection_parameters).create()
            logging.info("Snowflake session created successfully.")
        except Exception as e:
            logging.error(f"Failed to create Snowflake session: {e}")
            raise

        # Define view and mirror table names
        cls.view_name = "TEST_RANDOM_VIEW"
        cls.mirror_table_name = "TEST_MIRROR_TABLE"
        cls.final_mirror_parquet_path = "final_mirror_table.parquet"
        cls.debug_csv_path = "debug.csv"

        # Create a Snowflake view with multiple data types
        cls.create_random_view()

        # Track initialization status
        cls.mirror_initialized = False

    @classmethod
    def create_random_view(cls):
        """
        Create a Snowflake view that generates random data with multiple columns of different data types.
        """
        create_view_sql = f"""
        CREATE OR REPLACE VIEW {cls.view_name} AS
        SELECT 
            LPAD(SEQ4()::STRING, 6, '0') AS patient_id,  -- String column (6-digit patient_id)
            RANDOM() * 100 AS random_float,              -- Float column
            SEQ4() AS random_int,                        -- Integer column
            CONCAT('Name_', SEQ4()::STRING) AS name,     -- String column (Name)
            CURRENT_TIMESTAMP() AS created_at            -- Timestamp column
        FROM TABLE(GENERATOR(ROWCOUNT => 10))
        """
        cls.session.sql(create_view_sql).collect()
        logging.info(
            f"Random view {cls.view_name} created with multiple columns of different data types."
        )

    def monkey_patch_update(self, flux_state, simulated_date):
        """
        Monkey patch the update_mirror_table method using a PatchedDateTime class.
        """
        class PatchedDateTime(datetime):
            """
            A subclass of built-in `datetime.datetime` that overrides `now()` to use a fixed date.
            This preserves isinstance(value, datetime), so it won't break type checks.
            """
            _fixed_date = simulated_date

            @classmethod
            def now(cls, tz=None):
                if tz:
                    return tz.fromutc(cls._fixed_date)
                return cls._fixed_date

        try:
            with patch("fluxstate.datetime", PatchedDateTime):
                flux_state.update_mirror_table()
            logging.info("Mirror table updated successfully with simulated date.")
        except Exception as e:
            logging.error(f"Error during monkey patch update: {e}")
            raise

    def test_flux_state_view_capture(self):
        # Run 10 cycles of capturing view changes
        initial_date = datetime(2023, 1, 1)
        
        for i in range(10):
            simulated_date = initial_date + timedelta(days=30 * i)
            logging.info(
                f"--- Cycle {i + 1} ({simulated_date.strftime('%Y-%m-%d %H:%M:%S')}) ---"
            )

            # Step 1: Read the view data from Snowflake
            view_df = pl.DataFrame(self.session.table(self.view_name).to_pandas())
            logging.info("View data downloaded from Snowflake.")

            # Log the first few rows of the DataFrame to inspect the table structure and data
            logging.info(f"Head of view_df (first 5 rows):\n{view_df.head()}")
            logging.info(f"Data types of view_df:\n{view_df.dtypes}")

            if not self.mirror_initialized:
                # Step 2: Initialize FluxState with the view data on the first cycle
                class PatchedDateTime(datetime):
                    _fixed_date = initial_date

                    @classmethod
                    def now(cls, tz=None):
                        if tz:
                            return tz.fromutc(cls._fixed_date)
                        return cls._fixed_date

                with patch("fluxstate.datetime", PatchedDateTime):
                    flux_state = FluxState(
                        table=view_df, key_column="PATIENT_ID", mode="init"
                    )
                logging.info(
                    "Initialized FluxState with PATIENT_ID as key column."
                )
                self.mirror_initialized = True
            else:
                # Step 3: Download the existing mirror table from Snowflake
                mirror_table_df = pl.DataFrame(
                    self.session.table(self.mirror_table_name).to_pandas()
                )
                logging.info("Mirror table downloaded from Snowflake.")

                # Log the first few rows of the mirror table
                logging.info(
                    f"Head of mirror_table_df (first 5 rows):\n{mirror_table_df.head()}"
                )
                logging.info(
                    f"Data types of mirror_table_df:\n{mirror_table_df.dtypes}"
                )

                # Re-initialize FluxState with the downloaded mirror table
                flux_state = FluxState(
                    table=mirror_table_df,
                    key_column="PATIENT_ID",
                    mode="compare",
                )
                logging.info(
                    "Re-initialized FluxState with the existing mirror data."
                )

                # Update the mirror table with new changes from the view
                flux_state.table = view_df
                self.monkey_patch_update(flux_state, simulated_date)

            # Step 4: Save and upload the updated mirror table to Snowflake
            flux_state.save_mirror_table(
                self.final_mirror_parquet_path, csv_path="final_mirror_table.csv"
            )
            final_mirror_df = pl.read_parquet(
                self.final_mirror_parquet_path
            ).to_pandas()

            # Convert JSON strings back to lists of dictionaries and validate structure
            for col in final_mirror_df.columns:
                if col == "PATIENT_ID":
                    continue
                if isinstance(final_mirror_df[col].iloc[0], str):
                    try:
                        final_mirror_df[col] = final_mirror_df[col].apply(
                            lambda x: json.loads(x) if x else []
                        )
                        # Ensure proper structure
                        final_mirror_df[col] = final_mirror_df[col].apply(
                            lambda entries: [
                                {
                                    "date": entry.get("date", ""),
                                    "value": entry.get("value", None)
                                }
                                for entry in (entries if isinstance(entries, list) else [entries])
                                if isinstance(entry, dict)
                            ]
                        )
                        # Convert back to JSON string for Snowflake storage
                        final_mirror_df[col] = final_mirror_df[col].apply(json.dumps)
                    except json.JSONDecodeError as e:
                        logging.error(f"JSON decoding failed for column {col}: {e}")
                        continue

            # Create table with appropriate column types
            create_table_sql = f"""
            CREATE OR REPLACE TABLE {self.mirror_table_name} (
                PATIENT_ID STRING,
                {', '.join(f'{col} VARIANT' for col in final_mirror_df.columns if col != 'PATIENT_ID')}
            )
            """
            self.session.sql(create_table_sql).collect()
            logging.info(f"Created table {self.mirror_table_name} with appropriate column types")

            # Upload the final mirror table to Snowflake
            self.session.write_pandas(
                final_mirror_df,
                self.mirror_table_name,
                auto_create_table=False,
                overwrite=True,
            )
            logging.info(
                f"Updated mirror table saved to Snowflake after cycle {i + 1}."
            )

            # Wait for 30 seconds before the next cycle
            sleep(30)

        # Save the final mirror table for debugging
        flux_state.save_mirror_table(self.final_mirror_parquet_path, csv_path=self.debug_csv_path)
        logging.info("Final mirror table saved for inspection.")

    @classmethod
    def tearDownClass(cls):
        cls.cleanup()

    @classmethod
    def cleanup(cls):
        try:
            if cls.session:
                logging.info("Cleaning up Snowflake test objects and local files.")
                cls.session.sql(f"DROP VIEW IF EXISTS {cls.view_name}").collect()
                cls.session.sql(
                    f"DROP TABLE IF EXISTS {cls.mirror_table_name}"
                ).collect()
                cls.session.close()
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")

        # Remove local files
        for file_path in [cls.final_mirror_parquet_path, cls.debug_csv_path]:
            if os.path.exists(file_path):
                os.remove(file_path)


if __name__ == "__main__":
    unittest.main()
