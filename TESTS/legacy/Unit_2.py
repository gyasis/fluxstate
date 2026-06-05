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

class TestFluxState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up logging for the test
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("test_flux_state.log"),
            ],
        )

        logging.info("Setting up test environment for FluxState.")

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
        cls.final_mirror_parquet_path = (
            "final_mirror_table.parquet"  # Final mirror table path
        )
        cls.debug_csv_path = "debug.csv"  # Debug CSV file path

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

            # Delete a random row
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

        with patch("fluxstate.datetime", PatchedDateTime):
            flux_state.update_mirror_table()

        # Log mirror table content after update
        logging.debug("Mirror table content after update:")
        for col in flux_state.mirror_table:
            for index, entry in enumerate(flux_state.mirror_table[col]):
                if isinstance(entry, list):
                    logging.debug(f"Column '{col}', Row {index}: {entry}")
                else:
                    logging.debug(f"Column '{col}', Row {index}: {type(entry)} - {entry}")

    def test_flux_state_cycle(self):
        # Step 1: Download the Snowflake table as a Polars DataFrame
        main_table_df = pl.DataFrame(self.session.table(self.table_name).to_pandas())
        logging.info("Main table downloaded from Snowflake.")

        # Step 2: Initialize FluxState using Polars DataFrame (in-memory processing)
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

            # Step 5: Monkey patch and process the updated table with FluxState (comparison)
            flux_state.table = (
                updated_table_df  # Update the table in FluxState
            )
            print(f"updated_table_df: {updated_table_df}")
            print(f"FluxState Mirror Table: {flux_state.mirror_table}")

            self.monkey_patch_update(flux_state, simulated_date)

            # Save the FluxState mirror table
            flux_state.save_mirror_table(self.final_mirror_parquet_path)
            logging.info(f"FluxState mirror table saved after cycle {i + 1}.")

            # Re-import the updated mirror table back to Snowflake
            loaded_flux_state = FluxState.load_mirror_table(self.final_mirror_parquet_path, key_column=self.key_column_name)
            final_mirror_df = pl.DataFrame(loaded_flux_state.mirror_table).to_pandas()

            # Ensure consistent data types
            for col in final_mirror_df.columns:
                if col == self.key_column_name:
                    final_mirror_df[col] = final_mirror_df[col].astype(int)
                else:
                    final_mirror_df[col] = final_mirror_df[col].apply(lambda x: [entry if isinstance(entry, dict) else {"date": "", "value": entry} for entry in x])

            self.session.write_pandas(final_mirror_df, self.mirror_table_name, auto_create_table=True, overwrite=True)
            logging.info(f"Updated mirror table saved to Snowflake after cycle {i + 1}.")

            sleep(1)  # Simulate a delay between cycles

        # Save the final FluxState mirror table for debugging
        flux_state.save_mirror_table(self.final_mirror_parquet_path)
        logging.info("Final FluxState mirror table saved for inspection.")

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
        # for file_path in [
        #    cls.final_mirror_parquet_path,
        #    cls.debug_csv_path,
        # ]:
        #    if os.path.exists(file_path):
        #        os.remove(file_path)


if __name__ == "__main__":
    unittest.main()
