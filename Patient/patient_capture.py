import sys
import os
from pathlib import Path
import polars as pl
from datetime import datetime
import logging
import json
from snowflake.snowpark import Session
import time
import orjson
import pyarrow as pa
import pyarrow.parquet as pq

# Add the parent directory of fluxstate to the system path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from fluxstate import FluxState

# Setup logging
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("debug.log"), logging.StreamHandler()],
)

# JSON log file path
LOG_FILE_PATH = "mirror_table_log.json"
MAX_RETRIES = 3


# Function to load the log data
def load_log():
    if not Path(LOG_FILE_PATH).exists():
        # Create the JSON log file with default structure if it doesn't exist
        initial_log_data = {"initialization": False, "runs": []}
        save_log(initial_log_data)
    with open(LOG_FILE_PATH, "r") as f:
        return json.load(f)


# Function to save the log data
def save_log(log_data):
    with open(LOG_FILE_PATH, "w") as f:
        json.dump(log_data, f, indent=4)


# Function to check if the script has already run successfully today
def has_run_today(log_data):
    today = datetime.now().date().isoformat()
    for run in log_data["runs"]:
        if run["timestamp"].startswith(today):
            if run["status"] == "comparison_successful":
                return True
    return False


# Function to get the number of retries attempted today
def retries_today(log_data):
    today = datetime.now().date().isoformat()
    retries = 0
    for run in log_data["runs"]:
        if run["timestamp"].startswith(today) and "comparison_failed" in run["status"]:
            retries += 1
    return retries


# Function to format runtime into a human-readable format
def format_runtime(seconds):
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
    elif minutes > 0:
        return f"{int(minutes)}m {int(seconds)}s"
    else:
        return f"{int(seconds)}s"


# Snowflake connection parameters
        connection_parameters = {
            "account": os.environ.get("SNOWFLAKE_ACCOUNT"),
            "user": os.environ.get("SNOWFLAKE_USER"),
            "password": os.environ.get("SNOWFLAKE_PASSWORD"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "HERSELF_DEFAULT_XSMALL"),
            "database": os.environ.get("SNOWFLAKE_DATABASE", "TWICE"),
        }


def initialize_mirror_table(session):
    logging.info("Initializing the mirror table.")
    source_df = pl.DataFrame(session.table("HERSELF.HH_DMART.PATIENT").to_pandas())

    # Initialize the mirror table using FluxState
    flux_state = FluxState(table=source_df, mode="init")

    # Save the initialized mirror table back to Snowflake
    session.write_pandas(
        pl.DataFrame(flux_state.mirror_table).to_pandas(),
        "PATIENT_MIRROR",
        auto_create_table=True,
        overwrite=True,
    )
    logging.info("Mirror table 'PATIENT_MIRROR' initialized and saved to Snowflake.")

    # Save the mirror table locally as a Parquet file
    local_parquet_path = "mirror_table_local.parquet"
    local_csv_path = "mirror_table_local.csv"
    flux_state.save_mirror_table(local_parquet_path, local_csv_path)

    # Log successful initialization run
    log_data["runs"].append(
        {
            "status": "initialization_successful",
            "timestamp": datetime.now().isoformat(),
        }
    )
    save_log(log_data)


def compare_mirror_table():
    start_time = time.time()  # Record start time
    try:
        # Create Snowflake session
        session = Session.builder.configs(connection_parameters).create()

        # Use the specified database and schema in Snowflake
        session.sql("USE DATABASE TWICE").collect()
        session.sql("USE SCHEMA MIRROR").collect()

        # Load the mirror and source tables
        mirror_df = pl.DataFrame(session.table("PATIENT_MIRROR").to_pandas())
        source_df = pl.DataFrame(session.table("HERSELF.HH_DMART.PATIENT").to_pandas())
        logging.info("Mirror and source tables loaded.")

        # Print initial data from the tables for diagnostic purposes
        print("Initial Mirror Table Data:")
        print(mirror_df.head(5))  # Print first 5 rows of the mirror table
        print("\nInitial Source Table Data:")
        print(source_df.head(5))  # Print first 5 rows of the source table

        # Initialize FluxState in comparison mode with the mirror data
        flux_state = FluxState(table=mirror_df, mode="compare")

        # Update the FluxState with the new source data without overwriting
        flux_state.table = source_df  # Assign new data to the table
        flux_state.update_mirror_table()  # Update mirror table based on the changes

        # Retrieve and log change statistics
        change_stats = flux_state.get_change_statistics()
        logging.info(f"Change Statistics: {change_stats}")

        # Save the updated mirror table back to Snowflake and locally
        updated_df = pl.DataFrame(flux_state.mirror_table).to_pandas()
        session.write_pandas(
            updated_df, "PATIENT_MIRROR", auto_create_table=False, overwrite=True
        )
        logging.info("Mirror table 'PATIENT_MIRROR' updated in Snowflake.")

        local_parquet_path = "mirror_table_local.parquet"
        local_csv_path = "mirror_table_local.csv"
        flux_state.save_mirror_table(local_parquet_path, local_csv_path)

        # Calculate total runtime
        end_time = time.time()
        runtime = end_time - start_time
        human_readable_runtime = format_runtime(runtime)

        # Log successful comparison run with runtime
        log_data["runs"].append(
            {
                "status": "comparison_successful",
                "timestamp": datetime.now().isoformat(),
                "runtime": human_readable_runtime,
            }
        )
        save_log(log_data)
        logging.info(
            f"Comparison run logged successfully. Total runtime: {human_readable_runtime}."
        )
        session.close()

    except Exception as e:
        # Calculate total runtime even on failure
        end_time = time.time()
        runtime = end_time - start_time
        human_readable_runtime = format_runtime(runtime)

        logging.error(f"Comparison failed: {e}")
        log_data["runs"].append(
            {
                "status": "comparison_failed",
                "timestamp": datetime.now().isoformat(),
                "runtime": human_readable_runtime,
                "error": str(e),
            }
        )
        save_log(log_data)
        logging.info(f"Comparison failed. Total runtime: {human_readable_runtime}.")
        session.close()
        raise


# Load the log data
log_data = load_log()

# Main execution flow
if log_data["initialization"] is False:
    # If the table has never been initialized, do so now
    session = Session.builder.configs(connection_parameters).create()
    session.sql("USE DATABASE TWICE").collect()
    session.sql("USE SCHEMA MIRROR").collect()
    initialize_mirror_table(session)
    log_data["initialization"] = True  # Mark initialization as complete
    save_log(log_data)
    session.close()
else:
    # For subsequent runs, check if a comparison has already been successful today
    if has_run_today(log_data):
        logging.info("Script has already run successfully today. Exiting.")
    else:
        retry_count = retries_today(log_data)
        if retry_count < MAX_RETRIES:
            session = Session.builder.configs(connection_parameters).create()
            session.sql("USE DATABASE TWICE").collect()
            session.sql("USE SCHEMA MIRROR").collect()
            compare_mirror_table()
            session.close()
        else:
            logging.info("Max retries reached for today. Exiting.")
