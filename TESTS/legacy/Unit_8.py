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


class TestFluxStateStress(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up logging for the test
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("test_flux_state_stress.log"),
            ],
        )

        logging.info("Setting up test environment for FluxState stress testing.")

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
        cls.num_rows = 100000  # Increased number of rows for stress testing
        cls.num_columns = 15   # Increased number of columns for stress testing
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
        )

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
        return df  # Return the dataframe for assertions

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

    @unittest.test
    def test_flux_state_cycle(self):
        # Step 1: Download the Snowflake table as a Polars DataFrame
        main_table_df = pl.DataFrame(self.session.table(self.table_name).to_pandas())
        logging.info("Main table downloaded from Snowflake.")
        
        # Validate initial data
        self.assertEqual(len(main_table_df), self.num_rows, "Initial data row count mismatch")
        self.assertEqual(len(main_table_df.columns), self.num_columns + 1, "Initial data column count mismatch")  # +1 for key column
        self.assertTrue(self.key_column_name in main_table_df.columns, "Key column missing from initial data")

        # Step 2: Initialize FluxState using Polars DataFrame (in-memory processing)
        initial_date = datetime(2020, 1, 1)  # Start from 2020 for extended time range
        
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

        # Simulate cycles of changes from January 2020 to May 2024
        start_date = datetime(2020, 1, 1)
        end_date = datetime(2024, 5, 1)
        current_date = start_date
        iteration = 0

        while current_date <= end_date:
            logging.info(
                f"--- Cycle {iteration + 1} ({current_date.strftime('%Y-%m-%d')}) ---"
            )

            # Step 3: Simulate changes in the Snowflake table based on the iteration
            self.simulate_snowflake_table_change(iteration=iteration)
            self.print_table(self.table_name, f"Main Table (After update {iteration + 1})")

            # Step 4: Download the updated Snowflake table as a Polars DataFrame
            updated_table_df = pl.DataFrame(
                self.session.table(self.table_name).to_pandas()
            )
            logging.info(
                f"Updated table DataFrame structure:\n{updated_table_df.head()}"
            )

            # Step 5: Update the table in FluxState and process changes
            flux_state.table = updated_table_df
            self.monkey_patch_update(flux_state, current_date)

            # Validate table update
            self.assertIsNotNone(flux_state.mirror_table, "Mirror table not created")
            self.assertEqual(
                len(flux_state.mirror_table.columns),
                len(updated_table_df.columns),
                "Column count mismatch after update"
            )

            # Step 6: Save and upload the updated mirror table to Snowflake
            flux_state.save_mirror_table(
                self.final_mirror_parquet_path, csv_path="final_mirror_table.csv"
            )
            final_mirror_df = pl.read_parquet(
                self.final_mirror_parquet_path
            ).to_pandas()

            # Validate Snowflake table structure
            snowflake_table = self.print_table(
                self.mirror_table_name, f"Mirror Table (After update {iteration + 1})"
            )
            self.assertIsNotNone(snowflake_table, "Snowflake table not created")
            self.assertTrue(self.key_column_name in snowflake_table.columns, "Key column missing in Snowflake table")

            # Move to the next month and increment iteration
            current_date += timedelta(days=30)
            iteration += 1
            sleep(1)  # Simulate a delay between cycles

        # Test time travel functionality with multiple dates
        test_dates = ["2020-06-01", "2021-12-31", "2023-06-01", "2024-03-01"]
        for travel_date in test_dates:
            snapshot_df = flux_state.travel(travel_date)
            logging.info(f"Snapshot as of {travel_date}:\n{snapshot_df}")
            print(f"Snapshot as of {travel_date}:\n{snapshot_df}")
            
            # Validate time travel results
            self.assertIsNotNone(snapshot_df, f"Time travel snapshot not created for {travel_date}")
            self.assertEqual(
                len(snapshot_df.columns),
                self.num_columns + 1,  # +1 for key column
                f"Column count mismatch in time travel snapshot for {travel_date}"
            )
            self.assertTrue(
                self.key_column_name in snapshot_df.columns,
                f"Key column missing in time travel snapshot for {travel_date}"
            )

        # Save the final mirror table for debugging
        flux_state.save_mirror_table(self.final_mirror_parquet_path, csv_path=self.debug_csv_path)
        logging.info("Final mirror table saved for inspection.")

        # Validate saved files
        self.assertTrue(os.path.exists(self.final_mirror_parquet_path), "Final parquet file not created")
        self.assertTrue(os.path.exists(self.debug_csv_path), "Debug CSV file not created")

        # Run filter tests
        self.run_filter_test(flux_state)

    def run_filter_test(self, flux_state):
        try:
            print("Starting filter test at the end of the cycles")

            # Test column filters with multiple values
            for test_value in ["25", "50", "75"]:
                column_filters = {"COLUMN_1": test_value}
                filtered_df = flux_state.filter(column_filters=column_filters)
                print(f"Applied column filters for value {test_value}")

                # Validate filter results
                self.assertIsNotNone(filtered_df, f"Filter result is None for value {test_value}")
                self.assertTrue(
                    self.key_column_name in filtered_df.columns,
                    f"Key column missing in filter result for value {test_value}"
                )

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
                                    str(record.get("value")), test_value,
                                    f"Filtered result does not match for COLUMN_1 at index {idx}."
                                )
                logging.info(f"Column filter test passed for value {test_value}")

            # Test date range filters with multiple ranges
            date_ranges = [
                ("2020-01-01", "2021-12-31"),
                ("2022-01-01", "2023-12-31"),
                ("2024-01-01", "2024-05-01")
            ]

            for date_range in date_ranges:
                filtered_df_date = flux_state.filter(date_range=date_range)
                print(f"Applied date range filter for {date_range}")

                # Validate date range filter results
                self.assertIsNotNone(
                    filtered_df_date,
                    f"Date range filter result is None for range {date_range}"
                )
                self.assertTrue(
                    self.key_column_name in filtered_df_date.columns,
                    f"Key column missing in date range filter result for range {date_range}"
                )

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
                                start_date = datetime.strptime(date_range[0], "%Y-%m-%d")
                                end_date = datetime.strptime(date_range[1], "%Y-%m-%d")
                                self.assertTrue(
                                    start_date <= record_date <= end_date,
                                    f"Filtered result date {record_date} not within range for index {idx}."
                                )
                logging.info(f"Date range filter test passed for range {date_range}")

            # Test combined filters with multiple combinations
            test_combinations = [
                ({"COLUMN_1": "50"}, ("2020-01-01", "2021-12-31")),
                ({"COLUMN_1": "75"}, ("2022-01-01", "2023-12-31")),
                ({"COLUMN_1": "25"}, ("2024-01-01", "2024-05-01"))
            ]

            for column_filters, date_range in test_combinations:
                combined_filtered_df = flux_state.filter(
                    column_filters=column_filters,
                    date_range=date_range
                )
                print(f"Combined filters applied for {column_filters} and {date_range}")

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
                                start_date = datetime.strptime(date_range[0], "%Y-%m-%d")
                                end_date = datetime.strptime(date_range[1], "%Y-%m-%d")
                                self.assertTrue(
                                    str(record.get("value")) == column_filters["COLUMN_1"] and
                                    start_date <= record_date <= end_date,
                                    f"Combined filter result does not match at index {idx}."
                                )
                logging.info(f"Combined filter test passed for {column_filters} and {date_range}")

            print("Completed filter test successfully")

        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            raise

    @unittest.test
    def test_simulate_snowflake_table_change(self):
        """Test that table changes are correctly simulated"""
        iteration = 0
        initial_df = self.print_table(self.table_name, "Initial Table")
        initial_row_count = len(initial_df)
        
        self.simulate_snowflake_table_change(iteration=iteration)
        updated_df = self.print_table(self.table_name, "Updated Table")
        
        # Validate changes
        self.assertEqual(
            len(updated_df.columns),
            len(initial_df.columns),
            "Column count changed after simulation"
        )
        self.assertTrue(
            self.key_column_name in updated_df.columns,
            "Key column missing after simulation"
        )

    @unittest.test
    def test_monkey_patch_update(self):
        """Test that datetime patching works correctly"""
        main_table_df = pl.DataFrame(self.session.table(self.table_name).to_pandas())
        flux_state = FluxState(
            table=main_table_df,
            key_column=self.key_column_name,
            mode="init",
        )
        
        test_date = datetime(2023, 1, 1)
        self.monkey_patch_update(flux_state, test_date)
        
        # Validate patching worked
        self.assertIsNotNone(flux_state.mirror_table, "Mirror table not created after patching")
        self.assertTrue(
            self.key_column_name in flux_state.mirror_table.columns,
            "Key column missing after patching"
        )

    @unittest.test
    def test_run_filter_test(self, flux_state):
        try:
            print("Starting filter test at the end of the cycles")

            # Test column filters with multiple values
            for test_value in ["25", "50", "75"]:
                column_filters = {"COLUMN_1": test_value}
                filtered_df = flux_state.filter(column_filters=column_filters)
                print(f"Applied column filters for value {test_value}")

                # Validate filter results
                self.assertIsNotNone(filtered_df, f"Filter result is None for value {test_value}")
                self.assertTrue(
                    self.key_column_name in filtered_df.columns,
                    f"Key column missing in filter result for value {test_value}"
                )

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
                                    str(record.get("value")), test_value,
                                    f"Filtered result does not match for COLUMN_1 at index {idx}."
                                )
                logging.info(f"Column filter test passed for value {test_value}")

            # Test date range filters with multiple ranges
            date_ranges = [
                ("2020-01-01", "2021-12-31"),
                ("2022-01-01", "2023-12-31"),
                ("2024-01-01", "2024-05-01")
            ]

            for date_range in date_ranges:
                filtered_df_date = flux_state.filter(date_range=date_range)
                print(f"Applied date range filter for {date_range}")

                # Validate date range filter results
                self.assertIsNotNone(
                    filtered_df_date,
                    f"Date range filter result is None for range {date_range}"
                )
                self.assertTrue(
                    self.key_column_name in filtered_df_date.columns,
                    f"Key column missing in date range filter result for range {date_range}"
                )

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
                                start_date = datetime.strptime(date_range[0], "%Y-%m-%d")
                                end_date = datetime.strptime(date_range[1], "%Y-%m-%d")
                                self.assertTrue(
                                    start_date <= record_date <= end_date,
                                    f"Filtered result date {record_date} not within range for index {idx}."
                                )
                logging.info(f"Date range filter test passed for range {date_range}")

            # Test combined filters with multiple combinations
            test_combinations = [
                ({"COLUMN_1": "50"}, ("2020-01-01", "2021-12-31")),
                ({"COLUMN_1": "75"}, ("2022-01-01", "2023-12-31")),
                ({"COLUMN_1": "25"}, ("2024-01-01", "2024-05-01"))
            ]

            for column_filters, date_range in test_combinations:
                combined_filtered_df = flux_state.filter(
                    column_filters=column_filters,
                    date_range=date_range
                )
                print(f"Combined filters applied for {column_filters} and {date_range}")

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
                                start_date = datetime.strptime(date_range[0], "%Y-%m-%d")
                                end_date = datetime.strptime(date_range[1], "%Y-%m-%d")
                                self.assertTrue(
                                    str(record.get("value")) == column_filters["COLUMN_1"] and
                                    start_date <= record_date <= end_date,
                                    f"Combined filter result does not match at index {idx}."
                                )
                logging.info(f"Combined filter test passed for {column_filters} and {date_range}")

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
