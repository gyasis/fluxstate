from datetime import datetime
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, field_validator, Field, ValidationError
import orjson

class HistoricalRecord(BaseModel):
    date: str  # We'll keep it as string since that's how it's stored
    value: Optional[str]  # Allow None/NULL values

    @field_validator('date', mode='before')
    @classmethod
    def validate_date_format(cls, v):
        """Convert datetime objects to strings and validate date format"""
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(v, str):
            try:
                # First try the standard format
                datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
                return v
            except ValueError:
                try:
                    # If that fails, try parsing ISO format and convert to standard format
                    dt = datetime.fromisoformat(v)
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    raise ValueError("Date must be in format YYYY-MM-DD HH:MM:SS or ISO format")
        else:
            raise ValueError("Date must be either a datetime object or a string")

    @field_validator('value', mode='before')
    @classmethod
    def convert_value_to_string(cls, v):
        """Convert any value to string format"""
        if v is None:
            return "NULL"
        elif isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(v, (int, float, bool)):
            return str(v)
        elif isinstance(v, str):
            return v
        else:
            return str(v)

class MirrorTableColumn(BaseModel):
    records: List[HistoricalRecord]

    @field_validator('records', mode='before')
    @classmethod
    def check_no_nested_lists(cls, v):
        if not isinstance(v, list):
            raise ValueError("Column data must be a list")
        
        if any(isinstance(item, list) for item in v):
            raise ValueError("Each historical record must be a dictionary")
        
        # Convert any non-dict records to dict format
        processed = []
        for item in v:
            if isinstance(item, dict):
                processed.append(item)
            else:
                # If it's not a dict, assume it's a value and create a record
                processed.append({
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "value": str(item)
                })
        return processed

class MirrorTableValidator:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def validate_structure(self, mirror_table: Dict[str, List[Any]], key_column: str) -> bool:
        """
        Validates the structure of the mirror table.
        
        Args:
            mirror_table: The mirror table to validate
            key_column: The name of the key column to exclude from validation
            
        Returns:
            bool: True if validation passes, False otherwise
        """
        try:
            for column_name, column_data in mirror_table.items():
                if column_name == key_column:
                    continue
                
                if not column_data:  # Skip empty columns
                    continue
                    
                for row in column_data:
                    if not row:  # Skip empty rows
                        continue
                    
                    # Validate the row structure
                    try:
                        validated_row = MirrorTableColumn(records=row)
                    except ValidationError as e:
                        self.logger.error(f"Validation failed for column {column_name}, row data: {row}")
                        self.logger.error(str(e))
                        return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Mirror table validation failed: {str(e)}")
            return False

    def _validate_column(self, column_name: str, column_data: List[Any]):
        try:
            MirrorTableColumn(records=column_data)
        except Exception as e:
            raise ValueError(f"Validation failed for column {column_name}: {str(e)}")

    def validate_before_upload(self, mirror_table: Dict[str, List], key_column: str) -> Dict[str, List]:
        """
        Validates the mirror table structure before uploading to ensure:
        1. No nested lists in the data
        2. Each record has proper date and value format
        3. All values are converted to strings
        
        Args:
            mirror_table: The mirror table to validate
            key_column: The name of the key column to exclude from validation
            
        Returns:
            Dict[str, List]: The validated and processed mirror table
        """
        processed_table = {}
        processed_table[key_column] = mirror_table[key_column]

        for column_name, column_data in mirror_table.items():
            if column_name == key_column:
                continue

            try:
                # Process each row in the column
                processed_records = []
                for row in column_data:
                    # Skip empty rows
                    if not row:
                        processed_records.append([])
                        continue

                    # Validate the row structure
                    validated_row = MirrorTableColumn(records=row)
                    # Convert Pydantic models back to dictionaries
                    processed_records.append([
                        {"date": record.date, "value": record.value}
                        for record in validated_row.records
                    ])

                processed_table[column_name] = processed_records
                logging.info(f"Successfully validated column: {column_name}")

            except ValidationError as e:
                logging.error(f"Validation failed for column {column_name}: {e}")
                raise

        return processed_table

    def validate_column(self, column_data: List) -> List:
        """
        Validates a single column of the mirror table.
        
        Args:
            column_data: List of historical records for a column
            
        Returns:
            List: The validated column data
        """
        validated_cells = []
        for cell in column_data:
            if not cell:  # Handle empty cells
                validated_cells.append([])
                continue

            try:
                validated_cell = MirrorTableColumn(records=cell)
                # Convert Pydantic models back to dictionaries
                validated_cells.append([
                    {"date": record.date, "value": record.value}
                    for record in validated_cell.records
                ])
            except ValidationError as e:
                logging.error(f"Cell validation failed: {e}")
                raise

        return validated_cells 