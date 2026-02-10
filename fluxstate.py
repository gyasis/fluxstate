# File: fluxstate.py
import polars as pl
from datetime import datetime
import json
from tqdm import tqdm
import logging
import pyarrow as pa
import pyarrow.parquet as pq
import numpy as np
import humanize
import orjson
from typing import Any, Dict, List
import pandas as pd
from mirror_validator import MirrorTableValidator

# Set up logging configuration
logging.basicConfig(level=logging.INFO)

def is_list_of_dicts(obj) -> bool:
    """Return True if obj is a list of dicts that each have 'date' and 'value' keys."""
    if not isinstance(obj, list):
        return False
    return all(
        isinstance(x, dict) and 'date' in x and 'value' in x
        for x in obj
    )

def flatten_if_needed(obj):
    """
    If obj is a list containing exactly one item that is itself a list of dicts,
    flatten it. This prevents [[{'date':..., 'value':...}]] from occurring.
    """
    if (isinstance(obj, list)
        and len(obj) == 1
        and isinstance(obj[0], list)
        and is_list_of_dicts(obj[0])):
        logging.debug("Flattening a double-nested list of dicts.")
        return obj[0]
    return obj

class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

def convert_to_string(value):
    if value is None:
        return "NULL"
    elif isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    else:
        return str(value).strip()

class FluxState:
    def __init__(self, table, key_column=None, mode="init", expect_serialized=False):
        self.table = table
        self.key_column = key_column or self.table.columns[0]
        self.validator = MirrorTableValidator()

        # Cast all columns except the key column to string
        self.table = self.table.with_columns([
            pl.col(col).cast(pl.Utf8) if col != self.key_column else pl.col(col)
            for col in self.table.columns
        ])

        logging.info(f"Initialized FluxState with table structure:\n{self.table.schema}")

        if mode == "init":
            self.mirror_table = self._initialize_mirror_table()
            logging.info("Mirror table initialized.")
        elif mode == "compare":
            if expect_serialized:
                self.mirror_table = self._initialize_mirror_from_serialized_table()
            else:
                self.mirror_table = self._initialize_mirror_from_existing_table()
            logging.info("Mirror table loaded for comparison.")

            table_shape = self.table.shape
            table_size_bytes = self.table.estimated_size()
            table_size_human = humanize.naturalsize(table_size_bytes)

            print(f"\n{'-'*40}")
            print(f"{'MIRROR TABLE LOADED':<20} {'SHAPE':<10} {'SIZE':<10}")
            print(f"{'-'*40}")
            print(f"{'':<20} {table_shape[0]},{table_shape[1]:<10} {table_size_human:<10}")
            print(f"{'-'*40}\n")
        else:
            raise ValueError(f"Unknown mode: {mode}")

        if not hasattr(self, "mirror_table") or self.mirror_table is None:
            raise AttributeError("mirror_table is not set in FluxState.")
    
    def _initialize_mirror_table(self):
        """
        Initializes the mirror table based on the main table.
        Returns:
            dict: The initialized mirror table.
        """
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mirror_table = {col: [] for col in self.table.columns}

        for row in self.table.iter_rows(named=True):
            for col in self.table.columns:
                if col == self.key_column:
                    mirror_table[col].append(row[col])
                    continue

                value_str = convert_to_string(row[col])
                try:
                    parsed = orjson.loads(value_str)
                    parsed = flatten_if_needed(parsed)
                    if is_list_of_dicts(parsed):
                        mirror_table[col].append(parsed)
                    else:
                        mirror_table[col].append([
                            {"date": current_date, "value": value_str}
                        ])
                except orjson.JSONDecodeError:
                    mirror_table[col].append([
                        {"date": current_date, "value": value_str}
                    ])

        logging.info("Mirror table structure initialized.")
        return mirror_table

    def _initialize_mirror_from_serialized_table(self):
        """
        Initializes the mirror table from a serialized table.
        Returns:
            dict: The initialized mirror table.
        """
        mirror_table = {col: [] for col in self.table.columns}
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for row in self.table.iter_rows(named=True):
            for col in self.table.columns:
                if col == self.key_column:
                    mirror_table[col].append(row[col])
                    continue

                cell_value = row[col]
                try:
                    deserialized_value = orjson.loads(cell_value)
                    deserialized_value = flatten_if_needed(deserialized_value)
                    if is_list_of_dicts(deserialized_value):
                        mirror_table[col].append(deserialized_value)
                    else:
                        mirror_table[col].append([
                            {"date": current_date, "value": str(cell_value)}
                        ])
                except orjson.JSONDecodeError:
                    mirror_table[col].append([
                        {"date": current_date, "value": str(cell_value)}
                    ])

        return mirror_table

    def _initialize_mirror_from_existing_table(self):
        """
        Initializes the mirror table from an existing table (for compare mode),
        ensuring that all values are stored as strings for consistent comparisons.
        Returns:
            dict: The mirror table initialized from the existing data.
        """
        mirror_table = {col: [] for col in self.table.columns}
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for row in self.table.iter_rows(named=True):
            for col in self.table.columns:
                if col == self.key_column:
                    mirror_table[col].append(row[col])
                    continue

                cell_value = row[col]
                try:
                    if isinstance(cell_value, str):
                        deserialized_value = orjson.loads(cell_value)
                        deserialized_value = flatten_if_needed(deserialized_value)
                        if is_list_of_dicts(deserialized_value):
                            mirror_table[col].append(deserialized_value)
                        else:
                            mirror_table[col].append([
                                {"date": current_date, "value": convert_to_string(deserialized_value)}
                            ])
                    else:
                        mirror_table[col].append([
                            {"date": current_date, "value": convert_to_string(cell_value)}
                        ])
                except (orjson.JSONDecodeError, TypeError) as e:
                    logging.debug(f"Failed to parse value in column {col}: {e}")
                    mirror_table[col].append([
                        {"date": current_date, "value": convert_to_string(cell_value)}
                    ])

        return mirror_table

    def update_mirror_table(self):
        """
        Updates the mirror table based on changes in the main table.
        Ensures that comparisons are made between strings to avoid false positives due to type differences.
        """
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Identify rows that exist in the mirror table but not in the main table
        mirror_ids = set(self.mirror_table[self.key_column])
        main_ids = set(self.table[self.key_column].to_list())
        
        # Handle deleted rows
        for id_ in mirror_ids - main_ids:
            index = self.mirror_table[self.key_column].index(id_)
            for col in self.table.columns:
                if col == self.key_column:
                    continue
                if not isinstance(self.mirror_table[col][index], list):
                    self.mirror_table[col][index] = []
                last_value = self.mirror_table[col][index][-1]["value"]
                if last_value is not None and last_value != "NULL":
                    self.mirror_table[col][index].append({
                        "date": current_date, 
                        "value": None
                    })
                logging.info(f"Row deleted for ID {id_} in column {col}.")

        # Handle new and existing rows
        for row in self.table.iter_rows(named=True):
            id_ = row[self.key_column]
            if id_ not in mirror_ids:
                # New row
                self.mirror_table[self.key_column].append(id_)
                for col in self.table.columns:
                    if col == self.key_column:
                        continue
                    new_value_str = convert_to_string(row[col])
                    try:
                        parsed = orjson.loads(new_value_str)
                        parsed = flatten_if_needed(parsed)
                        if is_list_of_dicts(parsed):
                            self.mirror_table[col].append(parsed)
                        else:
                            self.mirror_table[col].append([{
                                "date": current_date, 
                                "value": new_value_str
                            }])
                    except orjson.JSONDecodeError:
                        self.mirror_table[col].append([{
                            "date": current_date,
                            "value": new_value_str
                        }])
                logging.info(f"New row added for ID {id_}.")
            else:
                # Existing row
                index = self.mirror_table[self.key_column].index(id_)
                for col in self.table.columns:
                    if col == self.key_column:
                        continue
                    new_value_str = convert_to_string(row[col])
                    if not isinstance(self.mirror_table[col][index], list):
                        self.mirror_table[col][index] = []
                    last_entry = self.mirror_table[col][index][-1]

                    # Convert both values to strings before comparison
                    last_value_str = str(last_entry["value"]).strip()

                    if new_value_str != last_value_str:
                        self.mirror_table[col][index].append({
                            "date": current_date, 
                            "value": new_value_str
                        })
                    logging.info(
                        f"Value changed for ID {id_} in column {col}: "
                        f"{last_entry['value']} -> {new_value_str}"
                    )

    def save_mirror_table(self, output_path_parquet, csv_path=None):
        """
        Save the mirror table to a parquet file and optionally to a CSV file.
        
        Args:
            output_path_parquet (str): Path to save the parquet file
            csv_path (str, optional): Path to save the CSV file. If provided, saves to both parquet and CSV.
        """
        # Convert mirror table to DataFrame
        mirror_df = {}
        for col in self.mirror_table:
            if col == self.key_column:
                mirror_df[col] = self.mirror_table[col]
            else:
                mirror_df[col] = [orjson.dumps(value).decode('utf-8') for value in self.mirror_table[col]]
        
        df = pl.DataFrame(mirror_df)
        
        # Save as parquet
        df.write_parquet(output_path_parquet)
        logging.info(f"Mirror table saved to parquet: {output_path_parquet}")
        
        # Optionally save as CSV
        if csv_path:
            df.write_csv(csv_path)
            logging.info(f"Mirror table saved to CSV: {csv_path}")
            
        return df

    @classmethod
    def load_mirror_table(cls, parquet_path, key_column=None):
        """
        Load a mirror table from a parquet file.
        
        Args:
            parquet_path (str): Path to the parquet file
            key_column (str, optional): Name of the key column
            
        Returns:
            FluxState: A new FluxState instance with the loaded mirror table
        """
        df = pl.read_parquet(parquet_path)
        if key_column is None:
            key_column = df.columns[0]
            
        flux_state = cls(table=df, key_column=key_column, mode="compare", expect_serialized=True)
        return flux_state

    def query_historical_value(self, query_date):
        """
        Queries the historical value of a column for a given date.
        """
        query_date = datetime.strptime(query_date, "%Y-%m-%d")
        historical_values = {}

        logging.info(f"Querying historical values for date: {query_date}")

        for id_ in set(self.table[self.key_column].to_list()):
            historical_values[id_] = {}
            for col in self.table.columns:
                if col == self.key_column:
                    continue
                changes = self.mirror_table[col][
                    self.table[self.key_column].to_list().index(id_)
                ]
                filtered_changes = [
                    change
                    for change in changes
                    if datetime.strptime(change["date"], "%Y-%m-%d %H:%M:%S") <= query_date
                ]
                if filtered_changes:
                    historical_values[id_][col] = filtered_changes[-1]["value"]
                else:
                    historical_values[id_][col] = changes[0]["value"]

        return historical_values

    def travel(self, date):
        """
        Returns a snapshot of the table as it was at a given point in time, with fuzzy date matching.
        """
        try:
            query_date = datetime.fromisoformat(date)
        except ValueError:
            query_date = datetime.fromisoformat(f"{date}T23:59:59")

        snapshot_table = {col: [] for col in self.table.columns}

        for index in range(len(self.mirror_table[self.key_column])):
            for col in self.table.columns:
                if col == self.key_column:
                    snapshot_table[col].append(self.mirror_table[col][index])
                else:
                    historical_records = self.mirror_table[col][index]
                    if isinstance(historical_records, str):
                        try:
                            historical_records = orjson.loads(historical_records)
                        except ValueError as e:
                            logging.error(
                                f"Failed to decode JSON for column {col}, row {index}: {e}"
                            )
                            continue
                    value_at_time = None
                    for record in sorted(historical_records, key=lambda x: x["date"]):
                        record_date = datetime.fromisoformat(record["date"])
                        if record_date.date() == query_date.date():
                            value_at_time = record["value"]
                        elif record_date < query_date:
                            value_at_time = record["value"]
                        if record_date > query_date:
                            break
                    snapshot_table[col].append(value_at_time)

        snapshot_df = pl.DataFrame(snapshot_table)
        non_key_columns = [
            pl.col(col).is_null()
            for col in snapshot_df.columns
            if col != self.key_column
        ]
        snapshot_df = snapshot_df.filter(
            ~pl.fold(pl.lit(True), lambda acc, x: acc & x, non_key_columns)
        )

        return snapshot_df

    def get_change_statistics(self):
        """
        Gets statistics about the changes in the mirror table.
        """
        total_cells = len(self.table) * (len(self.table.columns) - 1)
        changed_cells = sum(
            len(changes)
            for col in self.mirror_table
            if col != self.key_column
            for changes in self.mirror_table[col]
            if isinstance(changes, list)
        )
        percent_changed = (changed_cells / total_cells) * 100 if total_cells else 0

        logging.info(
            f"Change statistics calculated: {percent_changed:.2f}% cells changed."
        )

        return {
            "total_cells": total_cells,
            "changed_cells": changed_cells,
            "percent_changed": percent_changed,
            "mirror_table_size": changed_cells,
        }

    def filter(self, column_filters=None, date_range=None):
        """
        Public interface for filtering the mirror table.
        
        Args:
            column_filters (dict, optional): A dictionary of column names and their corresponding filter values.
                                           Example: {"age": 30, "blood_pressure": 140}.
            date_range (tuple, optional): A tuple containing the start and end dates ("YYYY-MM-DD").
                                       Example: ("2023-01-01", "2024-01-01").
        
        Returns:
            dict: A filtered mirror table structure.
        """
        return self._filter(column_filters, date_range)

    def _filter(self, column_filters=None, date_range=None):
        """
        Internal method that filters the mirror table based on column values and/or a date range.
        Returns a new mirror table structure with the filtered data, always including the key column.
        """
        # Start with deserializing the columns
        deserialized_columns = {}
        for col in self.table.columns:
            if col != self.key_column:  # Skip the key column
                try:
                    deserialized_columns[col] = [
                        orjson.loads(entry) if isinstance(entry, str) else entry
                        for entry in self.mirror_table[col]
                    ]
                except Exception as e:
                    logging.error(f"Deserialization failed for column {col}: {e}")
                    continue

        # Initial print statement for all keys
        logging.debug(f"INITIAL FILTERED_KEYS: {set(self.mirror_table[self.key_column])}")

        # Step 1: Apply Date Range Filter if provided
        filtered_keys = set(self.mirror_table[self.key_column])  # Start with all keys
        date_filtered_columns = None
        if date_range:
            filtered_keys, date_filtered_columns = self._filter_by_date(
                deserialized_columns, date_range
            )
            deserialized_columns = date_filtered_columns  # Update deserialized columns with the filtered data
            logging.debug(f"FILTERED_KEYS AFTER DATE RANGE: {filtered_keys}")

        # Step 2: Apply Column Filters to the date-filtered data (if date filtering was applied)
        if column_filters:
            filtered_keys = self._filter_by_columns(
                deserialized_columns, column_filters, filtered_keys
            )

        logging.debug(f"FINAL FILTERED_KEYS: {filtered_keys}")

        # Step 3: Build the filtered mirror table with only the key column and filtered columns
        filtered_mirror_table = {self.key_column: []}

        # Only add the filtered columns if column_filters is not None
        if column_filters:
            for col in column_filters.keys():
                filtered_mirror_table[col] = []

        # Add all columns when filtering by date range only
        if date_range and not column_filters:
            for col in self.table.columns:
                if col != self.key_column:  # Avoid duplicate initialization
                    filtered_mirror_table[col] = []

        for idx, key in enumerate(self.mirror_table[self.key_column]):
            if key in filtered_keys:
                # Add the key column value
                filtered_mirror_table[self.key_column].append(key)
                # Add the filtered column values if column_filters is provided
                if column_filters:
                    for col in column_filters.keys():
                        if idx < len(deserialized_columns[col]):
                            filtered_mirror_table[col].append(
                                deserialized_columns[col][idx]
                            )
                elif date_range and not column_filters:
                    # If only filtering by date, include all columns in the final result
                    for col in deserialized_columns.keys():
                        if idx < len(date_filtered_columns[col]):
                            filtered_mirror_table[col].append(
                                date_filtered_columns[col][idx]
                            )

        return filtered_mirror_table

    def _filter_by_date(self, deserialized_columns, date_range):
        """
        Filters the mirror table based on a date range.
        Returns a set of filtered keys and filtered columns.
        """
        start_date, end_date = date_range
        date_filtered_keys = set()
        date_filtered_columns = {col: [] for col in self.table.columns}

        for idx, key in enumerate(self.mirror_table[self.key_column]):
            date_pass = False
            for col in deserialized_columns.keys():
                if isinstance(deserialized_columns[col][idx], list):
                    filtered_cell = [
                        entry
                        for entry in deserialized_columns[col][idx]
                        if isinstance(entry, dict)
                        and "date" in entry
                        and start_date <= entry["date"] <= end_date
                    ]
                    if filtered_cell:
                        date_pass = True
                        date_filtered_columns[col].append(filtered_cell)
                    else:
                        date_filtered_columns[col].append([])
                else:
                    date_filtered_columns[col].append([])

            if date_pass:
                date_filtered_keys.add(key)
            else:
                for col in deserialized_columns.keys():
                    date_filtered_columns[col].pop()  # Remove the last appended row

        return date_filtered_keys, date_filtered_columns

    def _filter_by_columns(self, deserialized_columns, column_filters, filtered_keys):
        """
        Filters the mirror table based on column values.
        Returns a set of filtered keys and updates the deserialized columns.
        """
        for col, filter_value in column_filters.items():
            if col in deserialized_columns:
                col_filtered_keys = set()
                for idx, key in enumerate(self.mirror_table[self.key_column]):
                    if key in filtered_keys:  # Only apply column filter to date-filtered keys
                        cell = deserialized_columns[col][idx]
                        if callable(filter_value):
                            filtered_entries = [
                                entry
                                for entry in cell
                                if entry.get("value") is not None
                                and filter_value(entry.get("value"))
                            ]
                        else:
                            filtered_entries = [
                                entry
                                for entry in cell
                                if entry.get("value") == filter_value
                            ]
                        # If there are filtered entries, keep the key; otherwise, exclude it
                        if filtered_entries:
                            col_filtered_keys.add(key)
                        deserialized_columns[col][idx] = filtered_entries  # Update the filtered column

                filtered_keys &= col_filtered_keys  # Further filter with column keys

        return filtered_keys

    def filter_for_null_values(self, column_name=None, date_range=None):
        """
        Filters for rows where any column (or a specific column) has dictionaries with null or None values
        within the specified date range. It retains only the dictionaries with null values and their corresponding dates.

        Args:
            column_name (str, optional): The name of the column to filter on for null or None values.
                                        If None, filters across all columns.
            date_range (tuple, optional): A tuple containing the start and end dates ("YYYY-MM-DD")
                                        with optional times. If time is not provided, defaults to
                                        start time 00:00:00 and end time 23:59:59.

        Returns:
            dict: A filtered mirror table structure with the same structure but only containing null values.
        """
        # Parse the date range if provided
        start_date, end_date = None, None
        if date_range:
            # Parse start date and time
            start_date = (
                datetime.strptime(date_range[0], "%Y-%m-%d %H:%M:%S")
                if " " in date_range[0]
                else datetime.strptime(date_range[0], "%Y-%m-%d")
            )
            if (
                len(date_range[0]) == 10
            ):  # Only date provided, default to start of the day
                start_date = start_date.replace(hour=0, minute=0, second=0)

            # Parse end date and time
            end_date = (
                datetime.strptime(date_range[1], "%Y-%m-%d %H:%M:%S")
                if " " in date_range[1]
                else datetime.strptime(date_range[1], "%Y-%m-%d")
            )
            if (
                len(date_range[1]) == 10
            ):  # Only date provided, default to end of the day
                end_date = end_date.replace(hour=23, minute=59, second=59)

        # Step 1: Initialize the filtered mirror table
        filtered_mirror_table = {self.key_column: self.mirror_table[self.key_column]}

        # Step 2: Determine which columns to filter
        columns_to_filter = [column_name] if column_name else self.table.columns

        # Step 3: Iterate over the specified columns and filter for null values
        for col in columns_to_filter:
            if col == self.key_column:
                continue  # Skip the key column

            deserialized_column = []
            if col in self.table.columns:
                try:
                    deserialized_column = [
                        orjson.loads(entry) if isinstance(entry, str) else entry
                        for entry in self.mirror_table[col]
                    ]
                except Exception as e:
                    logging.error(f"Deserialization failed for column {col}: {e}")
                    filtered_mirror_table[col] = []
                    continue

            # Filter for null values and apply date range filtering if specified
            null_only_column = []
            for cell in deserialized_column:
                if isinstance(cell, list):
                    # Keep only dictionaries where the "value" is None and the date is within the date range (if specified)
                    null_entries = [
                        entry
                        for entry in cell
                        if entry.get("value") is None
                        and (
                            not date_range
                            or (
                                start_date
                                <= datetime.strptime(
                                    entry.get("date"), "%Y-%m-%d %H:%M:%S"
                                )
                                <= end_date
                            )
                        )
                    ]
                    null_only_column.append(null_entries)
                else:
                    null_only_column.append(
                        []
                    )  # If it's not a list, set it as an empty list

            filtered_mirror_table[col] = null_only_column

        return filtered_mirror_table
