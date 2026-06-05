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
from mirror_validator import MirrorTableValidator
from changelog import ChangeLogStore, DELETED_FIELD
import reconstruct

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
    def __init__(self, table, key_column=None, mode="init", expect_serialized=False,
                 store_path=None):
        self.table = table
        self.key_column = key_column or self.table.columns[0]
        self.validator = MirrorTableValidator()

        # Preserve the ORIGINAL typed snapshot for change-log capture (type fidelity,
        # API-4). The legacy in-memory mirror below stringifies everything; the store
        # must see real dtypes, so we keep the source frame before that cast.
        self._source_table = table

        # Bind the append-only change-log store. `store_path` is an additive keyword
        # (back-compat: existing positional/keyword calls are unaffected). Defaults to
        # `fluxstate.flux/` in the cwd so the pathless quickstart usage works.
        self.store = ChangeLogStore(store_path or "fluxstate.flux")

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

    def update_mirror_table(self, captured_at=None):
        """Capture the current snapshot into the append-only change-log (FR-011 / API-1).

        **Behavior change, signature preserved.** Instead of mutating an in-memory
        mirror, this delegates to :meth:`ChangeLogStore.capture`: a keyed join-diff
        (row-hash prefiltered) of the typed source snapshot vs prior state appends
        exactly one immutable ``events/<ts>.parquet`` and commits the manifest.
        O(rows), no full-table rewrite, idempotent on re-capture of the same
        snapshot.

        ``captured_at`` is an additive optional keyword (back-compat: the legacy
        no-arg call still works) that lets callers stamp a deterministic capture
        time; defaults to now.

        Returns the capture result dict (``events_added`` / ``snapshot_id`` /
        ``file`` / ``noop``).
        """
        return self.store.capture(
            self._source_table, self.key_column, captured_at=captured_at
        )

    # --- reconstruction primitives re-exported as thin methods (U2 / T019) --- #
    def as_of(self, entity_id, field, T):
        """Value of ``(entity_id, field)`` as of ``T`` (decoded to its dtype)."""
        return reconstruct.as_of(self.store, entity_id, field, T)

    def get_timeline(self, entity_id, field=None):
        """Per-cell timeline ``[{date, value}]`` (typed) for an entity (and field)."""
        return reconstruct.get_timeline(self.store, entity_id, field=field)

    def change_count(self, entity_id):
        """Number of change events recorded for an entity."""
        return reconstruct.change_count(self.store, entity_id)

    def row_state(self, entity_id, T="now"):
        """Lifecycle state ``{state, resurrected}`` of an entity at ``T``."""
        return reconstruct.row_state(self.store, entity_id, T)

    def save_mirror_table(self, output_path_parquet=None, csv_path=None, *, output_format=None):
        """Return/write the reconstructed mirror view in the requested format (FR-012 / API-3).

        Additive ``output_format`` keyword. **Precedence (I1)** — resolves the
        back-compat-vs-default conflict:

        1. explicit ``output_format`` → honor exactly (``polars`` → ``pl.DataFrame``,
           ``arrow`` → ``pa.Table``, ``parquet``/``csv`` → write & return the path).
        2. else ``output_path_parquet`` given → write parquet, return its path (legacy).
        3. else ``csv_path`` given → write CSV, return its path (legacy).
        4. else → return a ``pl.DataFrame`` (the convenient default).

        Errors (``ValueError``): an unknown explicit ``output_format``; a file
        format requested without its path.
        """
        view = reconstruct.build_mirror_view(self.store, T="now")
        valid = ("polars", "arrow", "parquet", "csv")

        if output_format is not None:
            fmt = str(output_format).lower()
            if fmt not in valid:
                raise ValueError(f"unknown output_format {output_format!r}; valid: {list(valid)}")
            if fmt == "polars":
                return view
            if fmt == "arrow":
                return view.to_arrow()  # zero-copy pa.Table
            if fmt == "parquet":
                if not output_path_parquet:
                    raise ValueError("output_format='parquet' requires output_path_parquet")
                view.write_parquet(output_path_parquet)
                logging.info(f"Mirror view saved to parquet: {output_path_parquet}")
                return output_path_parquet
            # fmt == "csv"
            if not csv_path:
                raise ValueError("output_format='csv' requires csv_path")
            view.write_csv(csv_path)
            logging.info(f"Mirror view saved to CSV: {csv_path}")
            return csv_path

        # No explicit format → preserve legacy positional behavior.
        if output_path_parquet:
            view.write_parquet(output_path_parquet)
            logging.info(f"Mirror view saved to parquet: {output_path_parquet}")
            if csv_path:
                view.write_csv(csv_path)
                logging.info(f"Mirror view saved to CSV: {csv_path}")
            return output_path_parquet
        if csv_path:
            view.write_csv(csv_path)
            logging.info(f"Mirror view saved to CSV: {csv_path}")
            return csv_path
        return view

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
        """Historical state as of ``query_date`` (UTC), reconstructed from the change-log.

        Signature preserved (API-6). Returns ``{entity_id: {column: value}}`` with
        values restored to their original dtype (no string-cast). Built on
        :func:`reconstruct.build_mirror_view`.
        """
        logging.info(f"Querying historical values for date: {query_date}")
        view = reconstruct.build_mirror_view(self.store, T=query_date)
        result = {}
        non_key = [c for c in view.columns if c != self.key_column]
        for row in view.iter_rows(named=True):
            result[row[self.key_column]] = {c: row[c] for c in non_key}
        return result

    def travel(self, date):
        """Reconstructed table state **as of** ``date`` (UTC-compared), as a ``pl.DataFrame``.

        Signature preserved (API-4/API-6). Built on
        :func:`reconstruct.build_mirror_view`; ``date`` before any history returns
        an empty frame (not an error).
        """
        return reconstruct.build_mirror_view(self.store, T=date)

    def get_change_statistics(self):
        """Change statistics computed over the change-log (FR-011 / API-6).

        Signature preserved; now derived from committed change events and the live
        reconstructed view rather than the legacy in-memory mirror.
        """
        events = self.store._read_all_events()
        view = reconstruct.build_mirror_view(self.store, T="now")
        n_fields = max(len(view.columns) - 1, 0)
        total_cells = view.height * n_fields
        changed_cells = int(events.filter(pl.col("field") != DELETED_FIELD).height)
        percent_changed = (changed_cells / total_cells) * 100 if total_cells else 0

        logging.info(f"Change statistics: {percent_changed:.2f}% cells changed.")
        return {
            "total_cells": total_cells,
            "changed_cells": changed_cells,
            "percent_changed": percent_changed,
            "mirror_table_size": changed_cells,
        }


    def filter(self, column_filters=None, date_range=None):
        """Filter the reconstructed view by column values / date (FR-011 / API-6).

        Signature preserved; now returns a filtered ``pl.DataFrame`` of the
        reconstructed mirror view (as-of ``date_range`` end when given) instead of
        the legacy in-memory dict structure. ``column_filters`` maps a column to a
        value (equality) or a predicate callable.
        """
        return self._filter(column_filters, date_range)

    def _filter(self, column_filters=None, date_range=None):
        """Internal: reconstruct the view (optionally as-of the date range end) and
        apply the column filters, returning a ``pl.DataFrame``."""
        T = date_range[1] if date_range else "now"
        view = reconstruct.build_mirror_view(self.store, T=T)
        if column_filters:
            for col, val in column_filters.items():
                if col not in view.columns:
                    continue
                if callable(val):
                    view = view.filter(
                        pl.col(col).map_elements(
                            lambda x, _f=val: bool(_f(x)), return_dtype=pl.Boolean
                        )
                    )
                else:
                    view = view.filter(pl.col(col) == val)
        return view

    def filter_for_null_values(self, column_name=None, date_range=None):
        """Rows of the reconstructed view with a null value (FR-011 / API-6).

        Signature preserved; returns a ``pl.DataFrame`` of rows where ``column_name``
        is null (or, when omitted, where any non-key column is null), as of the
        ``date_range`` end when provided.
        """
        T = date_range[1] if date_range else "now"
        view = reconstruct.build_mirror_view(self.store, T=T)
        if column_name and column_name in view.columns:
            return view.filter(pl.col(column_name).is_null())
        non_key = [c for c in view.columns if c != self.key_column]
        if not non_key:
            return view.head(0)
        return view.filter(pl.any_horizontal([pl.col(c).is_null() for c in non_key]))
