import snowflake.snowpark as snowpark
import pandas as pd
from fluxstate import FluxState  # Ensure you import the FluxState class
import logging
from datetime import datetime

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


def create_hcc_tracking_table(session: snowpark.Session):
    # Step 1: Capture the original table with HCC tracking logic
    original_table = session.sql("""
        -- Your SQL logic here (as provided earlier)
    """).collect()

    # Step 2: Convert the result into a Pandas DataFrame
    original_df = pd.DataFrame(original_table)

    # Processing the data as per your steps (applying HCC processing and others)
    # You can retain your existing logic here, for example:
    original_df['BILLING_HCC_CODES'] = original_df['BILLING_CODES'].apply(convert_to_hcc)
    original_df['HCC_ON_BILL'] = original_df.apply(
        lambda row: check_hcc_on_bill(row['HCC_CODE'], row['BILLING_HCC_CODES']), axis=1)
    
    # More processing code ...

    # Step 3: Use FluxState to initialize or update the mirror table
    # Define the table name for the mirror
    mirror_table_name = "TWICE.MIRROR.HCC_TRACKING"
    key_column_name = "PATIENT_ID"

    # Initialize FluxState with the processed dataframe
    flux_state = FluxState(
        table=original_df, 
        key_column=key_column_name, 
        mode="init"  # Use "init" mode for initializing the mirror table
    )
    
    # Save the mirror table back to Snowflake
    session.write_pandas(flux_state.mirror_table, "HCC_TRACKING", auto_create_table=True, overwrite=True)

    logging.info("HCC Tracking mirror table created and saved to Snowflake.")

    return flux_state


def main(session: snowpark.Session):
    """
    Main function to manage the HCC Tracking table and update the mirror table.
    """
    # Parse table name for the mirror table
    mirror_table_name = "TWICE.MIRROR.HCC_TRACKING"
    database, schema, table = parse_table_name(mirror_table_name)

    # Ensure the schema is in use
    session.sql(f"USE SCHEMA {database}.{schema}").collect()

    # Step 4: Create the HCC Tracking table and initialize the mirror table
    create_hcc_tracking_table(session)

if __name__ == "__main__":
    # Example of how you'd initialize your Snowpark session and run the main logic
    connection_parameters = {
        "account": "your_account",
        "user": "your_user",
        "password": "your_password",
        "warehouse": "your_warehouse",
        "role": "your_role",
    }
    session = snowpark.Session.builder.configs(connection_parameters).create()

    main(session)
    session.close()
