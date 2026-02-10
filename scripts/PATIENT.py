# %%

import sys
import os
import time
import numpy as np
import polars as pl
from snowflake.snowpark import Session
import argparse  # Add argparse for command-line arguments
import logging
import pandas as pd

# Add the parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluxstate import FluxState

# temp fix

import json




def parse_table_name(full_table_name):
    """
    Parse the full table name to extract the database, schema, and table name.

    Parameters:
    full_table_name (str): The full table name in the format "DATABASE.SCHEMA.TABLE".

    Returns:
    tuple: A tuple containing the database, schema, and table name.
    """
    parts = full_table_name.split(".")
    if len(parts) != 3:
        raise ValueError("Table name must be in the format 'DATABASE.SCHEMA.TABLE'")
    return parts[0], parts[1], parts[2]


def main(mode, deserialize=False):
    """
    Main function to initialize or compare the mirror tables in Snowflake.

    Parameters:
    mode (str): The mode to run the script in.
                "init" - to initialize the mirror table with data from the main table.
                "compare" - to compare and update the mirror table with changes from the main table.
    """
    # Create Snowpark session
            connection_parameters = {
            "account": os.environ.get("SNOWFLAKE_ACCOUNT"),
            "user": os.environ.get("SNOWFLAKE_USER"),
            "password": os.environ.get("SNOWFLAKE_PASSWORD"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "HERSELF_DEFAULT_XSMALL"),
            "database": os.environ.get("SNOWFLAKE_DATABASE", "TWICE"),
        }
    session = Session.builder.configs(connection_parameters).create()

    # Define table names
    # table_name = 'TWICE.MIRROR."MIRROR.PATIENT_MIRROR"'
    table_name = "HERSELF.HH_DMART.PATIENT"
    key_column_name = "PATIENT_ID"
    mirror_table_name = "TWICE.MIRROR.PATIENT_MIRROR"

    # Parse the mirror table name
    database, schema, table = parse_table_name(mirror_table_name)

    # Set the schema
    session.sql(f"USE SCHEMA {database}.{schema}").collect()

    if mode == "init":
        # Load data from Snowflake table

        main_table_df = pl.DataFrame(session.table(table_name).to_pandas())

        # Count total rows before dropping duplicates
        total_rows = main_table_df.shape[0]

        # Drop duplicate rows based on PATIENT_ID, keeping the first occurrence
        main_table_df = main_table_df.unique(subset=["PATIENT_ID"], keep="first")

        # Count rows after dropping duplicates
        unique_rows = main_table_df.shape[0]

        # Calculate and log the number of dropped rows
        dropped_rows = total_rows - unique_rows
        logging.info(f"Dropped {dropped_rows} rows with duplicate PATIENT_ID")

        # Initialize FluxState with the main table in "init" mode
        flux_state = FluxState(
            table=main_table_df, key_column=key_column_name, mode="init"
        )
        df = pl.DataFrame(flux_state.mirror_table)
        df2 = pd.DataFrame(flux_state.mirror_table)

        # Check for data integrity using Polars DataFrame
        try:
            logging.info("Polars DataFrame head:")
            logging.info(df.head().to_pandas().to_string())
            if (
                not isinstance(df["PREFERRED_NAME"][0], list)
                or not df["PREFERRED_NAME"][0]
            ):
                logging.warning(f"Polars - Cell value: {df['PREFERRED_NAME'][0]}")
                logging.warning(
                    """
                ***************************************
                *                                     *
                *     CELL IS NOT A LIST OR EMPTY     *
                *                                     *
                ***************************************
                """
                )
                logging.info(df["PREFERRED_NAME"][0])
            else:
                cell_value = df["PREFERRED_NAME"][0]
                if any(item is None for item in cell_value) or any(
                    item == "NULL" for item in cell_value
                ):
                    logging.info(
                        "Polars - PREFERRED_NAME contains None or 'NULL'. Test passed."
                    )
                else:
                    logging.info(f"Polars - PREFERRED_NAME value: {cell_value}")
                logging.info(
                    """
                ***************************************
                *                                     *
                *    POLARS DATAFRAME PASSED TEST!    *
                *                                     *
                ***************************************
                """
                )
        except Exception as e:
            logging.error(f"Error processing Polars DataFrame: {str(e)}")

        # Check for data integrity using Pandas DataFrame
        try:
            logging.info("Pandas DataFrame head:")

            if (
                not isinstance(df2["PREFERRED_NAME"][0], list)
                or not df2["PREFERRED_NAME"][0]
                or "value" not in df2["PREFERRED_NAME"][0][0]
            ):
                logging.warning(f"Pandas - Cell value: {df2['PREFERRED_NAME'][0]}")
                logging.warning(
                    """
                ***************************************
                *                                     *
                * VALUE KEY NOT FOUND IN CELL DATA!   *
                *                                     *
                ***************************************
                """
                )
                logging.info(df2["PREFERRED_NAME"][0])
            else:
                logging.info(
                    f"Pandas - PREFERRED_NAME value: {df2['PREFERRED_NAME'][0][0]['value']}"
                )
                logging.info(
                    """
                ***************************************
                *                                     *
                *    PANDAS DATAFRAME PASSED TEST!    *
                *                                     *
                ***************************************
                """
                )
        except Exception as e:
            logging.error(f"Error processing Pandas DataFrame: {str(e)}")

        # Save initial data to Snowflake
        session.write_pandas(
            df.to_pandas(), table, auto_create_table=True, overwrite=True
        )
        logging.info("Initial data saved to Snowflake.")

    if mode == "compare":
        # Compare mode
        main_table_df = pl.DataFrame(session.table(table_name).to_pandas())
        # Count total rows before dropping duplicates
        total_rows = main_table_df.shape[0]

        # Drop duplicate rows based on PATIENT_ID, keeping the first occurrence
        main_table_df = main_table_df.unique(subset=["PATIENT_ID"], keep="first")

        # Count rows after dropping duplicates
        unique_rows = main_table_df.shape[0]

        # Calculate and log the number of dropped rows
        dropped_rows = total_rows - unique_rows
        logging.info(f"Dropped {dropped_rows} rows with duplicate PATIENT_ID")

        # Load data into a Pandas DataFrame first
        mirror_table_pd = session.table(mirror_table_name).to_pandas()

        # Process JSON cells for each column in the Pandas DataFrame
        # for col in mirror_table_pd.columns[1:]:
        #     mirror_table_pd[col] = mirror_table_pd[col].apply(process_json_cell)

        # Convert the processed Pandas DataFrame to a Polars DataFrame
        mirror_table_df = pl.DataFrame(mirror_table_pd)

        # Pass the deserialize flag to FluxState
        flux_state = FluxState(
            table=mirror_table_df,
            key_column=key_column_name,
            mode="compare",
            expect_serialized=True,
        )
        flux_state.table = main_table_df
        flux_state.update_mirror_table()

        # Get the updated mirror table as a Polars DataFrame
        updated_mirror_df = flux_state.save_mirror_table(output_format="polars")

        try:
            flux_state.save_mirror_table(
                "backup/mirror_table.parquet", "backup/mirror_table.csv"
            )
        except Exception as e:
            print(f"Error saving mirror table: {str(e)}")

        # Write the updated mirror table back to Snowflake
        session.write_pandas(
            updated_mirror_df.to_pandas(), table, auto_create_table=True, overwrite=True
        )
        logging.info("Updated mirror table saved to Snowflake.")

    session.close()

    # Output some information for comparison
    print("Main Table Head:")
    print(flux_state.table.head())

    print("Mirror Table Head:")
    print(pl.DataFrame(flux_state.mirror_table).head())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Initialize or compare the mirror tables."
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Initialize the mirror table with data from the main table.",
    )
    parser.add_argument(
        "--deserialize",
        action="store_true",
        help="Expect the mirror table from Snowflake to be serialized.",
    )
    args = parser.parse_args()

    # Set mode based on the presence of the --init flag
    mode = "init" if args.init else "compare"
    deserialize = args.deserialize
    main(mode, deserialize)

# %%
