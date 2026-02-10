# %%

import pandas as pd
import pyarrow.parquet as pq
import json


def verify_multiple_entries(parquet_file_path):
    # Load the Parquet file into a DataFrame
    df = pd.read_parquet(parquet_file_path)

    # List to store patient IDs with more than one entry in any cell
    patients_with_multiple_entries = []

    # Iterate over each row in the DataFrame
    for index, row in df.iterrows():
        multiple_entries_found = False

        # Check each column except 'PATIENT_ID'
        for col in df.columns:
            if col != "PATIENT_ID":
                cell_content = row[col]
                # Attempt to deserialize if the content is a string
                if isinstance(cell_content, str):
                    try:
                        cell_content = json.loads(cell_content)
                    except json.JSONDecodeError:
                        continue  # If it's not a valid JSON string, skip this cell

                # Check if the content is a list and has more than one entry
                if isinstance(cell_content, list) and len(cell_content) > 1:
                    multiple_entries_found = True
                    break

        # If a column with more than one entry was found, add the patient ID to the list
        if multiple_entries_found:
            patients_with_multiple_entries.append(row["PATIENT_ID"])

    return patients_with_multiple_entries


# Usage
parquet_file_path = (
    "/home/gyasis/Documents/code/Herself/tools/CDC/working/Patient/mirror_table.parquet"
)
resulting_patient_ids = verify_multiple_entries(parquet_file_path)
print("Patient IDs with multiple entries in any cell:")
print(resulting_patient_ids)


# %%
