import unittest
import numpy as np
import polars as pl
from datetime import datetime, timedelta
from time import sleep
import logging
import json
from unittest.mock import patch
import os
from snowflake.snowpark import Session
import sys
# Add the parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxstate import FluxState
import pandas as pd


class TestFluxStateTravel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up logging for the test
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("test_flux_state_travel.log"),
            ],
        )

        logging.info("Setting up test environment for FluxState with time travel.")

        # Create a Snowpark session for testing
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

        # Define test table names in Snowflake
        cls.table_name = "TEST_MAIN_TABLE"
        cls.mirror_table_name = "TEST_MIRROR_TABLE"
        cls.final_mirror_parquet_path = "final_mirror_table.parquet"
        cls.debug_csv_path = "debug.csv"

        # Create initial data using Polars
        np.random.seed(42)
        cls.num_rows = 10
        cls.num_columns = 5
        cls.initial_data = np.random.randint(
            0, 100, size=(cls.num_rows, cls.num_columns)
        )
        cls.column_names = [f"COLUMN_{i}" for i in range(cls.num_columns)]

        # Dynamically create a key column and move it to the front
        cls.key_column_name = "KEY_COLUMN"
        cls.df = pl.DataFrame(cls.initial_data, schema=cls.column_names)
        cls.df = cls.df.with_columns(
            [pl.Series(name=cls.key_column_name, values=range(1, cls.num_rows + 1))]
        )
        cls.df = cls.df.select(
            [cls.key_column_name] + cls.column_names
        )  # Move the key column to the front

        # Save the initial data to Snowflake (convert to Pandas first)
        cls.session.write_pandas(
            cls.df.to_pandas(), cls.table_name, auto_create_table=True, overwrite=True
        )
        logging.info("Initial data saved to Snowflake.")

        # Track deleted key columns
        cls.deleted_key_columns = set()

    def print_table(self, table_name, title):
        """
        Helper method to print the contents of a Snowflake table for visualization.
        """
        logging.info(f"Fetching contents of table: {table_name}")
        df = self.session.table(table_name).to_pandas()
        logging.info(f"\n{title}\n{df.to_string(index=False)}")

    def simulate_snowflake_table_change(self, iteration):
        """
        Simulate changes in the Snowflake table based on the current iteration.
        """
        update_df = (
            self.df.to_pandas().copy()
        )  # Use Pandas for updates to match Snowflake interaction

        # Modify some existing rows
        for _ in range(np.random.randint(1, self.num_rows)):
            row = np.random.randint(0, self.num_rows)
            key_value = update_df.at[row, self.key_column_name]

            # Only modify the row if it hasn't been deleted
            if key_value not in self.deleted_key_columns:
                col = np.random.choice(self.column_names)
                update_df.at[row, col] = np.random.randint(0, 100)
            else:
                logging.info(
                    f"Skipping modification for deleted row with KEY_COLUMN {key_value}."
                )

        # Only add rows after the 4th iteration
        if iteration >= 4:
            new_key_value = update_df[self.key_column_name].max() + 1
            while new_key_value in self.deleted_key_columns:
                new_key_value += 1  # Ensure the key is unique

            new_row_data = np.random.randint(0, 100, size=(1, self.num_columns))
            new_row = {self.key_column_name: new_key_value}
            new_row.update(
                {col: new_row_data[0][i] for i, col in enumerate(self.column_names)}
            )
            new_row_df = pd.DataFrame([new_row])  # Create a DataFrame from the new row
            update_df = pd.concat([update_df, new_row_df], ignore_index=True)
            logging.info(f"Added new row with KEY_COLUMN {new_key_value}.")
            logging.debug(f"New row data: {new_row}")

        # Delete rows only between iterations 5 and 7
        if 5 <= iteration <= 7:
            row_to_delete = np.random.choice(update_df[self.key_column_name])
            logging.info(f"Deleting row with KEY_COLUMN {row_to_delete}.")
            update_df = update_df[update_df[self.key_column_name] != row_to_delete]
            self.deleted_key_columns.add(row_to_delete)  # Track the deleted key

        # Add and delete rows for the last few iterations (8-10)
        if iteration >= 8:
            # Add new row
            new_key_value = update_df[self.key_column_name].max() + 1
            while new_key_value in self.deleted_key_columns:
                new_key_value += 1  # Ensure the key is unique

            new_row_data = np.random.randint(0, 100, size=(1, self.num_columns))
            new_row = {self.key_column_name: new_key_value}
            new_row.update(
                {col: new_row_data[0][i] for i, col in enumerate(self.column_names)}
            )
            new_row_df = pd.DataFrame([new_row])  # Create a DataFrame from the new row
            update_df = pd.concat([update_df, new_row_df], ignore_index=True)
            logging.info(f"Added new row with KEY_COLUMN {new_key_value}.")

            # Delete a random row
            row_to_delete = np.random.choice(update_df[self.key_column_name])
            logging.info(f"Deleting row with KEY_COLUMN {row_to_delete}.")
            update_df = update_df[update_df[self.key_column_name] != row_to_delete]
            self.deleted_key_columns.add(row_to_delete)  # Track the deleted key

        # Reset index before saving to Snowflake
        update_df.reset_index(drop=True, inplace=True)

        # Save the updated data back to Snowflake
        self.session.write_pandas(
            update_df, self.table_name, auto_create_table=False, overwrite=True
        )
        logging.info("Simulated table changes saved to Snowflake.")

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

    def test_flux_state_cycle(self):
        # Step 1: Download the Snowflake table as a Polars DataFrame
        main_table_df = pl.DataFrame(self.session.table(self.table_name).to_pandas())
        logging.info("Main table downloaded from Snowflake.")

        # Step 2: Initialize FluxState using Polars DataFrame (in-memory processing)
        initial_date = datetime(2023, 1, 1)
        
        class PatchedDateTime(datetime):
            _fixed_date = initial_date

            @classmethod
            def now(cls, tz=None):
                if tz:
                    return tz.fromutc(cls._fixed_date)
                return cls._fixed_date

        with patch("fluxstate.datetime", PatchedDateTime):
            flux_state = FluxState(
                table=main_table_df,
                key_column=self.key_column_name,
                mode="init",
            )
        logging.info("Initialized FluxState.")

        # Simulate cycles of changes
        for i in range(10):
            simulated_date = datetime(2023, 1, 1) + timedelta(days=30 * i)
            logging.info(
                f"--- Cycle {i + 1} ({simulated_date.strftime('%Y-%m-%d')}) ---"
            )

            # Step 3: Simulate changes in the Snowflake table based on the iteration
            self.simulate_snowflake_table_change(iteration=i)
            self.print_table(self.table_name, f"Main Table (After update {i + 1})")

            # Step 4: Download the updated Snowflake table as a Polars DataFrame
            updated_table_df = pl.DataFrame(
                self.session.table(self.table_name).to_pandas()
            )
            logging.info(
                f"Updated table DataFrame structure:\n{updated_table_df.head()}"
            )

            # Step 5: Update the table in FluxState and process changes
            flux_state.table = updated_table_df
            self.monkey_patch_update(flux_state, simulated_date)

            # Step 6: Save and upload the updated mirror table to Snowflake
            flux_state.save_mirror_table(
                self.final_mirror_parquet_path, csv_path="final_mirror_table.csv"
            )
            final_mirror_df = pl.read_parquet(
                self.final_mirror_parquet_path
            ).to_pandas()

            # Convert JSON strings back to lists of dictionaries and validate structure
            for col in final_mirror_df.columns:
                if col == self.key_column_name:
                    final_mirror_df[col] = final_mirror_df[col].astype(int)
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
                {self.key_column_name} INTEGER,
                {', '.join(f'{col} VARIANT' for col in final_mirror_df.columns if col != self.key_column_name)}
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
            self.print_table(
                self.mirror_table_name, f"Mirror Table (After update {i + 1})"
            )

            sleep(1)  # Simulate a delay between cycles

        # Test time travel functionality
        travel_date = "2023-06-01"  # Example date to test travel method
        snapshot_df = flux_state.travel(travel_date)
        logging.info(f"Snapshot as of {travel_date}:\n{snapshot_df}")
        print(f"Snapshot as of {travel_date}:\n{snapshot_df}")

        # Save the final mirror table for debugging
        flux_state.save_mirror_table(self.final_mirror_parquet_path, csv_path=self.debug_csv_path)
        logging.info("Final mirror table saved for inspection.")

        # Run filter tests
        self.run_filter_test(flux_state)

    def run_filter_test(self, flux_state):
        try:
            print("Starting filter test at the end of the cycles")

            # Test column filters
            column_filters = {"COLUMN_1": "50"}
            filtered_df = flux_state.filter(column_filters=column_filters)
            print("Applied column filters")

            # Verify column filter results
            for idx, value in enumerate(filtered_df["COLUMN_1"]):
                if value:
                    if isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError:
                            continue

                    if not isinstance(value, list):
                        continue

                    for record in value:
                        if isinstance(record, dict):
                            self.assertEqual(
                                str(record.get("value")), "50",
                                f"Filtered result does not match for COLUMN_1 at index {idx}."
                            )
            logging.info("Column filter test passed.")

            # Test date range filter
            date_range = ("2023-01-01", "2024-12-31")
            filtered_df_date = flux_state.filter(date_range=date_range)
            print("Applied date range filter")

            # Verify date range filter results
            for idx, changes in enumerate(filtered_df_date["COLUMN_1"]):
                if changes:
                    if isinstance(changes, str):
                        try:
                            changes = json.loads(changes)
                        except json.JSONDecodeError:
                            continue

                    if not isinstance(changes, list):
                        continue

                    for record in changes:
                        if isinstance(record, dict):
                            record_date = datetime.strptime(
                                record.get("date", "1970-01-01 00:00:00"),
                                "%Y-%m-%d %H:%M:%S"
                            )
                            self.assertTrue(
                                datetime(2023, 1, 1) <= record_date <= datetime(2024, 12, 31),
                                f"Filtered result date {record_date} not within range for index {idx}."
                            )
            logging.info("Date range filter test passed.")

            # Test combined filters
            combined_filtered_df = flux_state.filter(
                column_filters={"COLUMN_1": "50"},
                date_range=("2023-01-01", "2024-12-31")
            )
            print("Combined filters applied")

            # Verify combined filter results
            for idx, value in enumerate(combined_filtered_df["COLUMN_1"]):
                if value:
                    if isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError:
                            continue

                    if not isinstance(value, list):
                        continue

                    for record in value:
                        if isinstance(record, dict):
                            record_date = datetime.strptime(
                                record.get("date", "1970-01-01 00:00:00"),
                                "%Y-%m-%d %H:%M:%S"
                            )
                            self.assertTrue(
                                str(record.get("value")) == "50" and
                                datetime(2023, 1, 1) <= record_date <= datetime(2024, 12, 31),
                                f"Combined filter result does not match at index {idx}."
                            )
            logging.info("Combined filter test passed.")

            print("Completed filter test successfully")

        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            raise

    @classmethod
    def tearDownClass(cls):
        cls.cleanup()

    @classmethod
    def cleanup(cls):
        try:
            if cls.session:
                logging.info("Cleaning up Snowflake test tables and local files.")
                cls.session.sql(f"DROP TABLE IF EXISTS {cls.table_name}").collect()
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
