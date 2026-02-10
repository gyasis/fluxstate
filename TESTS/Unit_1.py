import sys
import os
import unittest
import numpy as np
import polars as pl
from datetime import datetime, timedelta
from time import sleep
import logging
from unittest.mock import patch
from snowflake.snowpark import Session

# Add the parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluxstate import FluxState

class TestFluxState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                            handlers=[logging.StreamHandler(), logging.FileHandler("test_flux_state.log")])
        logging.info("Setting up test environment for FluxState.")

        # Create Snowpark session
                connection_parameters = {
            "account": os.environ.get("SNOWFLAKE_ACCOUNT"),
            "user": os.environ.get("SNOWFLAKE_USER"),
            "password": os.environ.get("SNOWFLAKE_PASSWORD"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "HERSELF_DEFAULT_XSMALL"),
            "database": os.environ.get("SNOWFLAKE_DATABASE", "TWICE"),
        }
        cls.session = Session.builder.configs(connection_parameters).create()
        logging.info("Snowflake session created successfully.")

        # Define test table names and paths
        cls.table_name = "TEST_MAIN_TABLE"
        cls.mirror_table_name = "TEST_MIRROR_TABLE"
        cls.final_mirror_parquet_path = "final_mirror_table.parquet"
        cls.debug_csv_path = "debug.csv"

        # Create initial data
        cls.num_rows, cls.num_columns = 10, 5
        cls.initial_data = np.random.randint(0, 100, size=(cls.num_rows, cls.num_columns), dtype=np.int8)
        cls.column_names = [f"COLUMN_{i}" for i in range(cls.num_columns)]
        cls.key_column_name = "COLUMN_0"  # Set the key column name to COLUMN_0
        
        # Create the DataFrame with all columns
        cls.df = pl.DataFrame(cls.initial_data, schema=cls.column_names)
        
        # Ensure COLUMN_0 is unique and sorted
        cls.df = cls.df.with_columns([
            pl.Series(name=cls.key_column_name, values=range(1, cls.num_rows + 1)).cast(pl.Int8)
        ])
        
        # No need to select columns again, as they are already in the correct order
        # Remove this line:
        # cls.df = cls.df.select([cls.key_column_name] + cls.column_names)

        # Save initial data to Snowflake
        cls.session.write_pandas(cls.df.to_pandas(), cls.table_name, auto_create_table=True, overwrite=True)
        logging.info("Initial data saved to Snowflake.")

        cls.deleted_key_columns = set()

    def simulate_snowflake_table_change(self, iteration):
        # Simulate changes to the Snowflake table
        current_df = pl.DataFrame(self.session.table(self.table_name).to_pandas())
        
        # Randomly modify some values (excluding the key column)
        for col in self.column_names[1:]:  # Start from COLUMN_1
            mask = np.random.choice([True, False], size=len(current_df), p=[0.2, 0.8])
            current_df = current_df.with_columns(
                pl.when(mask)
                .then(pl.lit(np.random.randint(0, 100, dtype=np.int8)))
                .otherwise(pl.col(col))
                .alias(col)
            )
        
        # Randomly add a new row
        if np.random.random() < 0.3:
            new_row = {}
            new_key = max(current_df[self.key_column_name]) + 1
            for col in self.column_names:
                if col == self.key_column_name:
                    new_row[col] = int(new_key)
                else:
                    new_row[col] = np.random.randint(0, 100, dtype=np.int8)
            
            # Create a new DataFrame with the new row and cast its columns to match the types of the existing DataFrame
            new_df = pl.DataFrame([new_row]).with_columns([
                pl.col(col).cast(current_df[col].dtype) for col in current_df.columns
            ])
            current_df = current_df.vstack(new_df)
        
        # Randomly delete a row
        if np.random.random() < 0.2 and len(current_df) > 1:
            row_to_delete = np.random.choice(current_df[self.key_column_name])
            current_df = current_df.filter(pl.col(self.key_column_name) != row_to_delete)
            self.deleted_key_columns.add(row_to_delete)
        
        # Update the Snowflake table
        self.session.write_pandas(current_df.to_pandas(), self.table_name, overwrite=True)
        logging.info(f"Simulated changes applied to Snowflake table in iteration {iteration}")

    def monkey_patch_update(self, flux_state, simulated_date):
        with patch("fluxstate.datetime") as mock_datetime:
            mock_datetime.now.return_value = simulated_date
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            flux_state.update_mirror_table()

    def test_flux_state_cycle(self):
        main_table_df = pl.DataFrame(self.session.table(self.table_name).to_pandas())
        flux_state = FluxState(table=main_table_df, key_column=self.key_column_name, mode="init")
        logging.info("Initialized FluxState.")

        for i in range(10):
            simulated_date = datetime(2023, 1, 1) + timedelta(days=30 * i)
            logging.info(f"--- Cycle {i + 1} ({simulated_date.strftime('%Y-%m-%d')}) ---")

            self.simulate_snowflake_table_change(iteration=i)
            updated_table_df = pl.DataFrame(self.session.table(self.table_name).to_pandas())
            flux_state.table = updated_table_df

            self.monkey_patch_update(flux_state, simulated_date)

            # Save the FluxState mirror table
            flux_state.save_mirror_table(self.final_mirror_parquet_path, csv_path="final_mirror_table.csv")
            logging.info(f"FluxState mirror table saved after cycle {i + 1}.")

            # Re-import the updated mirror table back to Snowflake
            loaded_flux_state = FluxState.load_mirror_table(self.final_mirror_parquet_path, key_column=self.key_column_name)
            final_mirror_df = pl.DataFrame(loaded_flux_state.mirror_table).to_pandas()

            self.session.write_pandas(final_mirror_df, self.mirror_table_name, auto_create_table=True, overwrite=True)
            logging.info(f"Updated mirror table saved to Snowflake after cycle {i + 1}.")

            sleep(1)  # Simulate a delay between cycles

        # Save the final FluxState mirror table for debugging
        flux_state.save_mirror_table(self.final_mirror_parquet_path, csv_path=self.debug_csv_path)
        logging.info("Final FluxState mirror table saved for inspection.")

    @classmethod
    def tearDownClass(cls):
        if cls.session:
            logging.info("Cleaning up Snowflake test tables and local files.")
            cls.session.sql(f"DROP TABLE IF EXISTS {cls.table_name}").collect()
            cls.session.sql(f"DROP TABLE IF EXISTS {cls.mirror_table_name}").collect()
            cls.session.close()

if __name__ == "__main__":
    unittest.main()
