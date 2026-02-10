import sys
import os
from pathlib import Path
import polars as pl
from datetime import datetime, timedelta
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("debug.log"), logging.StreamHandler()],
)

# JSON log file path
LOG_FILE_PATH = "mirror_table_log.json"
MAX_RETRIES = 3


def load_log():
    """Load the log data from JSON file."""
    if not Path(LOG_FILE_PATH).exists():
        # Create the JSON log file with default structure if it doesn't exist
        initial_log_data = {"initialization": False, "runs": []}
        save_log(initial_log_data)
    with open(LOG_FILE_PATH, "r") as f:
        return json.load(f)


def save_log(log_data):
    """Save the log data to a JSON file."""
    with open(LOG_FILE_PATH, "w") as f:
        json.dump(log_data, f, indent=4)


def has_run_today(log_data):
    """Check if the script has already run successfully today."""
    today = datetime.now().date().isoformat()
    for run in log_data["runs"]:
        if run["timestamp"].startswith(today):
            if run["status"] == "comparison_successful":
                return True
    return False


def retries_today(log_data):
    """Get the number of retries attempted today."""
    today = datetime.now().date().isoformat()
    retries = 0
    for run in log_data["runs"]:
        if run["timestamp"].startswith(today) and "comparison_failed" in run["status"]:
            retries += 1
    return retries


def format_runtime(seconds):
    """Format runtime into a human-readable format."""
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


def initialize_flux_state(session, table_name, key_column="PATIENT_ID"):
    """Initialize the FluxState with data from Snowflake."""
    logging.info("Initializing the mirror table.")
    source_df = pl.DataFrame(session.table(table_name).to_pandas())

    # Convert all columns to string except the key column
    source_df = source_df.with_columns(
        [
            pl.col(col).cast(pl.Utf8) if col != key_column else pl.col(col)
            for col in source_df.columns
        ]
    )

    # Log the conversion process
    logging.info(
        f"Converted all columns to string except the key column '{key_column}'."
    )
    flux_state = FluxState(table=source_df, key_column=key_column, mode="init")

    # Save the initialized mirror table back to Snowflake
    session.write_pandas(
        pl.DataFrame(flux_state.mirror_table).to_pandas(),
        "PATIENT_MIRROR",
        auto_create_table=True,
        overwrite=True,
    )

    flux_state.save_mirror_table("mirror_table.parquet", "mirror_table.csv")

    # Log successful initialization
    log_data["runs"].append(
        {"status": "initialization_successful", "timestamp": datetime.now().isoformat()}
    )
    save_log(log_data)

    logging.info("Mirror table 'PATIENT_MIRROR' initialized and saved to Snowflake.")
    return flux_state


def compare_mirror_table(session, flux_state, mirror_table_name):
    """Compare the current state of the table with the mirror table and update it."""
    start_time = time.time()
    try:
        logging.info("Loading mirror and source tables from Snowflake.")
        mirror_df = pl.DataFrame(session.table(mirror_table_name).to_pandas())
        source_df = pl.DataFrame(session.table("HERSELF.HH_DMART.PATIENT").to_pandas())

        # Log the data types and shapes of the data frames
        logging.info(f"Mirror table columns: {mirror_df.dtypes}")
        logging.info(f"Source table columns: {source_df.dtypes}")
        logging.info(f"Mirror table shape: {mirror_df.shape}")
        logging.info(f"Source table shape: {source_df.shape}")

        logging.info("Initializing FluxState for comparison.")
        flux_state.table = source_df

        # Log before updating
        logging.info(f"Before update, mirror table:\n{flux_state.mirror_table}")

        # Update the mirror table using the FluxState method
        flux_state.update_mirror_table()

        # Log after updating
        logging.info(f"After update, mirror table:\n{flux_state.mirror_table}")

        # Save updated mirror table back to Snowflake
        updated_df = pl.DataFrame(flux_state.mirror_table).to_pandas()
        session.write_pandas(
            updated_df, mirror_table_name, auto_create_table=False, overwrite=True
        )

        end_time = time.time()
        runtime = end_time - start_time
        log_data["runs"].append(
            {
                "status": "comparison_successful",
                "timestamp": datetime.now().isoformat(),
                "runtime": format_runtime(runtime),
            }
        )
        save_log(log_data)
        logging.info(
            f"Comparison run logged successfully. Total runtime: {format_runtime(runtime)}."
        )

    except Exception as e:
        end_time = time.time()
        runtime = end_time - start_time
        logging.error(f"Comparison failed: {e}")
        log_data["runs"].append(
            {
                "status": "comparison_failed",
                "timestamp": datetime.now().isoformat(),
                "runtime": format_runtime(runtime),
                "error": str(e),
            }
        )
        save_log(log_data)
        logging.info(f"Comparison failed. Total runtime: {format_runtime(runtime)}.")
        raise


def create_snowflake_session():
    """Create a Snowflake session and set the correct database and schema."""
    try:
        logging.info("Creating Snowflake session...")
        session = Session.builder.configs(connection_parameters).create()
        session.sql("USE DATABASE TWICE").collect()
        session.sql("USE SCHEMA MIRROR").collect()
        logging.info("Snowflake session created and context set successfully.")
        return session
    except Exception as e:
        logging.error(f"Failed to create Snowflake session: {e}")
        raise


def mirror_table_exists(session, mirror_table_name):
    """Check if the mirror table exists in Snowflake."""
    try:
        result = session.sql(f"SHOW TABLES LIKE '{mirror_table_name}'").collect()
        exists = len(result) > 0
        logging.info(f"Mirror table '{mirror_table_name}' existence check: {exists}")
        return exists
    except Exception as e:
        logging.error(f"Error checking for mirror table existence: {e}")
        return False


# Main Execution Flow
if __name__ == "__main__":
    log_data = load_log()
    session = create_snowflake_session()

    # Correctly reference the table with the schema and table name
    table_name = "HERSELF.HH_DMART.PATIENT"

    mirror_table_name = "PATIENT_MIRROR"
    key_column = "PATIENT_ID"  # Use PATIENT_ID as the key column

    try:
        if not mirror_table_exists(session, mirror_table_name):
            flux_state = initialize_flux_state(session, table_name, key_column)
            log_data["initialization"] = True
            save_log(log_data)
        else:
            flux_state = FluxState.load_mirror_table("mirror_table.parquet", key_column)
            flux_state.table = pl.DataFrame(session.table(table_name).to_pandas())

            if has_run_today(log_data):
                logging.info("Script has already run successfully today. Exiting.")
            else:
                retry_count = retries_today(log_data)
                if retry_count < MAX_RETRIES:
                    compare_mirror_table(session, flux_state, mirror_table_name)
                else:
                    logging.info("Max retries reached for today. Exiting.")
    finally:
        session.close()
        logging.info("Snowflake session closed.")
