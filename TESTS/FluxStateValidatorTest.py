# File: FluxStateValidatorTest.py
import unittest
import numpy as np
import polars as pl
from datetime import datetime, timedelta
import logging
import json
from unittest.mock import patch
import os
from snowflake.snowpark import Session
import sys
import pandas as pd

# Add the parent directory to sys.path so we can import fluxstate.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluxstate import FluxState
# Import HistoricalRecord directly from mirror_validator instead of from FluxState
from mirror_validator import HistoricalRecord

class TestFluxStateAndValidator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("test_flux_state_validator.log"),
            ],
        )
        logging.info("Setting up test environment for FluxState and Validator.")

        # Snowflake connection parameters (dummy or real as needed)
                connection_parameters = {
            "account": os.environ.get("SNOWFLAKE_ACCOUNT"),
            "user": os.environ.get("SNOWFLAKE_USER"),
            "password": os.environ.get("SNOWFLAKE_PASSWORD"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "HERSELF_DEFAULT_XSMALL"),
            "database": os.environ.get("SNOWFLAKE_DATABASE", "TWICE"),
        }
        cls.session = Session.builder.configs(connection_parameters).create()

        # Test table configuration
        cls.table_name = "TEST_MAIN_TABLE"
        cls.mirror_table_name = "TEST_MIRROR_TABLE"
        cls.final_mirror_parquet_path = "final_mirror_table.parquet"
        cls.debug_csv_path = "debug.csv"

        # Create test data
        cls.num_rows = 1000
        cls.num_columns = 10
        cls.initial_data = np.random.randint(0, 100, size=(cls.num_rows, cls.num_columns))
        cls.column_names = [f"COLUMN_{i}" for i in range(cls.num_columns)]
        cls.key_column_name = "KEY_COLUMN"

        # Initialize test DataFrame
        cls.df = pl.DataFrame(cls.initial_data, schema=cls.column_names)
        cls.df = cls.df.with_columns([
            pl.Series(name=cls.key_column_name, values=range(1, cls.num_rows + 1))
        ])
        cls.df = cls.df.select([cls.key_column_name] + cls.column_names)

        # Save initial data to Snowflake (optional for local tests)
        cls.session.write_pandas(
            cls.df.to_pandas(), cls.table_name, auto_create_table=True, overwrite=True
        )
        logging.info("Initial test data saved to Snowflake.")

    def test_step1_initialization_and_validation(self):
        """Step 1: Test initialization of FluxState and initial validation"""
        logging.info("Step 1: Testing initialization and validation")

        # Initialize FluxState
        flux_state = FluxState(
            table=self.df,
            key_column=self.key_column_name,
            mode="init"
        )

        # Test validator initialization
        self.assertIsNotNone(flux_state.validator)

        # Validate initial mirror table structure (pass key_column)
        is_valid = flux_state.validator.validate_structure(
            flux_state.mirror_table, 
            flux_state.key_column
        )
        self.assertTrue(is_valid)

    def test_step2_data_type_validation(self):
        """Step 2: Test data type validation for different column types"""
        logging.info("Step 2: Testing data type validation")

        # Create test data with various types
        test_data = {
            self.key_column_name: [1, 2, 3],
            "string_col": [
                [{"date": datetime.now().isoformat(), "value": "str1"}],
                [{"date": datetime.now().isoformat(), "value": "str2"}],
                [{"date": datetime.now().isoformat(), "value": "str3"}]
            ],
            "int_col": [
                [{"date": datetime.now().isoformat(), "value": 1}],
                [{"date": datetime.now().isoformat(), "value": 2}],
                [{"date": datetime.now().isoformat(), "value": 3}]
            ],
            "float_col": [
                [{"date": datetime.now().isoformat(), "value": 1.1}],
                [{"date": datetime.now().isoformat(), "value": 2.2}],
                [{"date": datetime.now().isoformat(), "value": 3.3}]
            ],
            "null_col": [
                [{"date": datetime.now().isoformat(), "value": None}],
                [{"date": datetime.now().isoformat(), "value": None}],
                [{"date": datetime.now().isoformat(), "value": None}]
            ]
        }

        flux_state = FluxState(
            table=self.df,
            key_column=self.key_column_name,
            mode="init"
        )

        # Test validation of different data types (pass key_column!)
        processed_table = flux_state.validator.validate_before_upload(
            test_data,
            flux_state.key_column
        )
        self.assertIsNotNone(processed_table)

    def test_step3_historical_record_validation(self):
        """Step 3: Test validation of historical records structure"""
        logging.info("Step 3: Testing historical record validation")

        # We no longer do FluxState.HistoricalRecord; 
        # we import HistoricalRecord directly from mirror_validator
        test_record = {
            "date": datetime.now(),
            "value": "test_value"
        }

        # Create a HistoricalRecord from mirror_validator
        record = HistoricalRecord(**test_record)
        self.assertEqual(str(record.value), "test_value")

    def test_step4_error_handling(self):
        """Step 4: Test error handling in validation"""
        logging.info("Step 4: Testing error handling")

        flux_state = FluxState(
            table=self.df,
            key_column=self.key_column_name,
            mode="init"
        )

        # Test invalid data structure
        # The validator expects each non-key column row to be a list of dicts with 'date' and 'value'
        invalid_data = {
            self.key_column_name: [1, 2, 3],
            "invalid_col": [1, "2", {"invalid": "structure"}]  # This structure is not a list of dicts
        }

        with self.assertRaises(Exception):
            # Must pass key_column
            flux_state.validator.validate_before_upload(
                invalid_data,
                flux_state.key_column
            )

    def test_step5_serialization(self):
        """Step 5: Test serialization and deserialization with validation"""
        logging.info("Step 5: Testing serialization")

        flux_state = FluxState(
            table=self.df,
            key_column=self.key_column_name,
            mode="init"
        )

        # Save and reload the mirror table
        flux_state.save_mirror_table(
            output_format="both",
            output_path_parquet=self.final_mirror_parquet_path,
            output_path_csv=self.debug_csv_path
        )

        # If no exception was raised, we check if the files exist
        self.assertTrue(os.path.exists(self.final_mirror_parquet_path))
        self.assertTrue(os.path.exists(self.debug_csv_path))

        # Validate the reloaded data from the parquet
        reloaded_df = pl.read_parquet(self.final_mirror_parquet_path)
        self.assertIsNotNone(reloaded_df)

    def test_step6_change_tracking(self):
        """Step 6: Test change tracking with validation"""
        logging.info("Step 6: Testing change tracking")

        flux_state = FluxState(
            table=self.df,
            key_column=self.key_column_name,
            mode="init"
        )

        # Modify some data
        modified_df = self.df.clone()
        modified_df = modified_df.with_columns([
            pl.col("COLUMN_0").map_elements(lambda x: x + 1)
        ])

        # Update and validate
        flux_state.table = modified_df
        flux_state.update_mirror_table()

        # Validate structure again, passing key_column
        is_valid = flux_state.validator.validate_structure(
            flux_state.mirror_table, 
            flux_state.key_column
        )
        self.assertTrue(is_valid)

    def test_step7_filtering(self):
        """Step 7: Test filtering with validation"""
        logging.info("Step 7: Testing filtering")

        flux_state = FluxState(
            table=self.df,
            key_column=self.key_column_name,
            mode="init"
        )

        # Test date range filtering
        date_range = (
            datetime.now().strftime("%Y-%m-%d"),
            (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        )

        filtered_table = flux_state.filter(date_range=date_range)
        # Validate structure, passing key_column
        is_valid = flux_state.validator.validate_structure(
            filtered_table,
            flux_state.key_column
        )
        self.assertTrue(is_valid)

    def test_step8_integration(self):
        """Step 8: Integration test with all components"""
        logging.info("Step 8: Testing full integration")

        # Initialize FluxState
        flux_state = FluxState(
            table=self.df,
            key_column=self.key_column_name,
            mode="init"
        )

        # 1. Modify data
        modified_df = self.df.clone()
        modified_df = modified_df.with_columns([
            pl.col("COLUMN_0").map_elements(lambda x: x + 1)
        ])

        # 2. Update mirror table
        flux_state.table = modified_df
        flux_state.update_mirror_table()

        # 3. Filter data
        filtered_table = flux_state.filter(
            date_range=(
                datetime.now().strftime("%Y-%m-%d"),
                (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            )
        )

        # 4. Save and validate
        flux_state.save_mirror_table(
            output_format="both",
            output_path_parquet=self.final_mirror_parquet_path,
            output_path_csv=self.debug_csv_path
        )

        # Final validation (pass key_column)
        is_valid = flux_state.validator.validate_structure(
            flux_state.mirror_table,
            flux_state.key_column
        )
        self.assertTrue(is_valid)

    def test_step9_multiple_rounds(self):
        """Step 9: Test multiple rounds of data changes with validation"""
        logging.info("Step 9: Testing multiple rounds of changes")

        # Initialize FluxState
        flux_state = FluxState(
            table=self.df,
            key_column=self.key_column_name,
            mode="init"
        )

        # Save initial state
        flux_state.save_mirror_table(
            output_format="both",
            output_path_parquet=self.final_mirror_parquet_path,
            output_path_csv=self.debug_csv_path
        )

        # Run 6 rounds of changes
        for round_num in range(6):
            logging.info(f"\nStarting round {round_num + 1}")
            
            # Simulate downloading new data by modifying the DataFrame
            modified_df = self.df.clone()
            
            # Make different types of changes each round
            if round_num % 3 == 0:
                # Update numeric values
                modified_df = modified_df.with_columns([
                    pl.col("COLUMN_0").map_elements(lambda x: x + round_num + 1),
                    pl.col("COLUMN_1").map_elements(lambda x: x * 2)
                ])
            elif round_num % 3 == 1:
                # Update string values
                modified_df = modified_df.with_columns([
                    pl.col("COLUMN_2").map_elements(lambda x: f"modified_{x}_{round_num}"),
                    pl.col("COLUMN_3").map_elements(lambda x: f"changed_{x}")
                ])
            else:
                # Set some values to None
                modified_df = modified_df.with_columns([
                    pl.col("COLUMN_4").map_elements(lambda x: None if x % 2 == 0 else x),
                    pl.col("COLUMN_5").map_elements(lambda x: None if x % 3 == 0 else x)
                ])

            # Create new FluxState instance for comparison
            flux_state = FluxState(
                table=modified_df,
                key_column=self.key_column_name,
                mode="compare"
            )

            # Update mirror table
            flux_state.update_mirror_table()

            # Validate structure
            is_valid = flux_state.validator.validate_structure(
                flux_state.mirror_table,
                flux_state.key_column
            )
            self.assertTrue(is_valid, f"Validation failed in round {round_num + 1}")

            # Save updated state
            flux_state.save_mirror_table(
                output_format="both",
                output_path_parquet=self.final_mirror_parquet_path,
                output_path_csv=self.debug_csv_path
            )

            # Verify historical values are maintained
            if round_num > 0:
                # Check that we can query historical values
                date_range = (
                    datetime.now().strftime("%Y-%m-%d"),
                    (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                )
                filtered_table = flux_state.filter(date_range=date_range)
                self.assertIsNotNone(filtered_table)
                
                # Validate filtered table structure
                is_valid = flux_state.validator.validate_structure(
                    filtered_table,
                    flux_state.key_column
                )
                self.assertTrue(is_valid, f"Filtered table validation failed in round {round_num + 1}")

            logging.info(f"Completed round {round_num + 1} successfully")

    def test_step10_customer_churn(self):
        """Step 10: Test real-world scenario with source table changes and mirror comparison"""
        logging.info("Step 10: Testing real-world customer churn scenarios")

        # Initialize source table (simulating database table)
        source_df = self.df.clone()
        
        # Initialize FluxState with initial data
        flux_state = FluxState(
            table=source_df,
            key_column=self.key_column_name,
            mode="init"
        )

        # Save initial mirror table
        flux_state.save_mirror_table(
            output_format="both",
            output_path_parquet=self.final_mirror_parquet_path,
            output_path_csv=self.debug_csv_path
        )

        # Track statistics
        initial_customer_count = len(source_df)
        next_customer_id = initial_customer_count + 1

        # Run 6 rounds simulating real-world changes
        for round_num in range(6):
            logging.info(f"\nStarting round {round_num + 1}")
            
            # 1. Simulate database changes by modifying source table
            
            # Add new customers (1-3 per round)
            new_customers = []
            num_new = np.random.randint(1, 4)
            for i in range(num_new):
                new_row = {
                    self.key_column_name: next_customer_id + i,
                    **{f"COLUMN_{j}": np.random.randint(0, 100) for j in range(self.num_columns)}
                }
                new_customers.append(new_row)
            next_customer_id += num_new
            
            # Create new customer DataFrame with same schema as source
            if new_customers:
                # Create DataFrame with explicit schema to match source_df
                schema = {self.key_column_name: pl.Int64}
                schema.update({f"COLUMN_{i}": pl.Int64 for i in range(self.num_columns)})
                new_df = pl.DataFrame(new_customers, schema=schema)
                source_df = pl.concat([source_df, new_df])
                logging.info(f"Added {num_new} new customers")

            # Remove some customers (simulate customers leaving)
            if round_num > 0:
                # Randomly select ~10% of customers to remove
                all_ids = source_df[self.key_column_name].to_list()
                num_to_remove = max(1, len(all_ids) // 10)
                ids_to_remove = np.random.choice(all_ids, num_to_remove, replace=False)
                source_df = source_df.filter(~pl.col(self.key_column_name).is_in(ids_to_remove))
                logging.info(f"Removed {num_to_remove} customers")

            # Modify some existing customer data
            if round_num % 2 == 0:
                # Update numeric columns (keep as integers)
                source_df = source_df.with_columns([
                    pl.col("COLUMN_0").map_elements(lambda x: x + round_num + 1),
                    pl.col("COLUMN_1").map_elements(lambda x: x * 2)
                ])
            else:
                # Update string columns (these will be converted to strings)
                source_df = source_df.with_columns([
                    pl.col("COLUMN_2").map_elements(lambda x: f"modified_{x}_{round_num}"),
                    pl.col("COLUMN_3").map_elements(lambda x: f"changed_{x}")
                ])

            # 2. Create new FluxState instance to compare with mirror
            flux_state = FluxState(
                table=source_df,
                key_column=self.key_column_name,
                mode="compare"
            )

            # 3. Update mirror table based on source changes
            flux_state.update_mirror_table()

            # 4. Validate mirror table structure
            is_valid = flux_state.validator.validate_structure(
                flux_state.mirror_table,
                flux_state.key_column
            )
            self.assertTrue(is_valid, f"Validation failed in round {round_num + 1}")

            # 5. Save updated mirror table
            flux_state.save_mirror_table(
                output_format="both",
                output_path_parquet=self.final_mirror_parquet_path,
                output_path_csv=self.debug_csv_path
            )

            # 6. Verify historical values are maintained
            if round_num > 0:
                date_range = (
                    datetime.now().strftime("%Y-%m-%d"),
                    (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                )
                filtered_table = flux_state.filter(date_range=date_range)
                self.assertIsNotNone(filtered_table)
                
                is_valid = flux_state.validator.validate_structure(
                    filtered_table,
                    flux_state.key_column
                )
                self.assertTrue(is_valid, f"Filtered table validation failed in round {round_num + 1}")

            # Log statistics
            logging.info(f"Statistics after round {round_num + 1}:")
            logging.info(f"Total customers in source: {len(source_df)}")
            logging.info(f"New customers added (cumulative): {next_customer_id - initial_customer_count - 1}")

        # Save final state to CSV with statistics
        final_csv_path = "TESTS/final_mirror_table_with_churn.csv"
        logging.info(f"\nSaving final mirror table to {final_csv_path}")
        
        mirror_df = pl.DataFrame({
            col: flux_state.mirror_table[col] 
            for col in flux_state.mirror_table.keys()
        })
        
        with open(final_csv_path, 'w') as f:
            f.write("# Final Mirror Table after 6 rounds of customer churn\n")
            f.write(f"# Initial Customers: {initial_customer_count}\n")
            f.write(f"# Final Customers in Source: {len(source_df)}\n")
            f.write(f"# Total New Customers Added: {next_customer_id - initial_customer_count - 1}\n")
            f.write("# Timestamp: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n\n")
        
        mirror_df.write_csv(final_csv_path, mode='a')
        logging.info("Final mirror table saved successfully")

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.session:
                logging.info("Cleaning up test environment.")
                cls.session.sql(f"DROP TABLE IF EXISTS {cls.table_name}").collect()
                cls.session.sql(f"DROP TABLE IF EXISTS {cls.mirror_table_name}").collect()
                cls.session.close()

            # Clean up local files if they exist
            for file_path in [cls.final_mirror_parquet_path, cls.debug_csv_path]:
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")


if __name__ == "__main__":
    unittest.main()
