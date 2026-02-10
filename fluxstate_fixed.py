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
                if isinstance(cell_value, str):
                    try:
                        deserialized_value = orjson.loads(cell_value)
                        deserialized_value = flatten_if_needed(deserialized_value)
                        if is_list_of_dicts(deserialized_value):
                            mirror_table[col].append(deserialized_value)
                        else:
                            mirror_table[col].append([
                                {"date": current_date, "value": convert_to_string(deserialized_value)}
                            ])
                    except orjson.JSONDecodeError:
                        mirror_table[col].append([
                            {"date": current_date, "value": convert_to_string(cell_value)}
                        ])
                else:
                    # Non-string -> wrap directly
                    mirror_table[col].append([
                        {"date": current_date, "value": convert_to_string(cell_value)}
                    ])

        return mirror_table 