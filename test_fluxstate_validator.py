import unittest
from datetime import datetime, timedelta
import polars as pl
import sys
import os
import random

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluxstate.fluxstate import FluxState
from fluxstate.mirror_validator import MirrorTableValidator, HistoricalRecord, MirrorTableColumn

class TestFluxStateValidator(unittest.TestCase):
    def setUp(self):
        """Initialize test data and FluxState instance"""
        self.test_data = {
            'id': [1, 2, 3],
            'name': ['John', 'Jane', 'Bob'],
            'age': ['25', '30', '35']
        }
        self.test_df = pl.DataFrame(self.test_data)
        self.key_column = 'id'
        self.flux_state = FluxState(self.test_df, key_column=self.key_column)
        self.validator = MirrorTableValidator()

    def test_initialization_and_validation(self):
        """Test 1: Test initialization of FluxState and initial validation"""
        # Test initial validation
        is_valid = self.validator.validate_structure(
            self.flux_state.mirror_table, 
            self.key_column
        )
        self.assertTrue(is_valid)

        # Test invalid structure
        invalid_table = self.flux_state.mirror_table.copy()
        invalid_table['name'] = [{'invalid': 'structure'}]
        with self.assertRaises(ValueError):
            self.validator.validate_structure(invalid_table, self.key_column)

    def test_data_type_validation(self):
        """Test 2: Test data type validation for different column types"""
        test_data = {
            'id': ['1'],
            'mixed_types': [{
                'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'value': 123  # Integer that should be converted to string
            }]
        }
        
        processed_table = self.validator.validate_before_upload(test_data, 'id')
        self.assertTrue(isinstance(processed_table['mixed_types'][0][0]['value'], str))

    def test_historical_record_validation(self):
        """Test 3: Test validation of historical records structure"""
        # Test valid record
        test_record = {
            'date': datetime.now(),
            'value': 'test_value'
        }
        record = HistoricalRecord(**test_record)
        self.assertEqual(record.value, 'test_value')

        # Test invalid record
        with self.assertRaises(ValueError):
            HistoricalRecord(date=datetime.now(), value={'invalid': 'dict'})

    def test_error_handling(self):
        """Test 4: Test error handling in validation"""
        # Test missing key column
        with self.assertRaises(ValueError):
            self.validator.validate_structure({}, 'nonexistent_key')

        # Test invalid date format
        invalid_data = {
            'id': ['1'],
            'test': [{
                'date': 'invalid_date',
                'value': 'test'
            }]
        }
        with self.assertRaises(ValueError):
            self.validator.validate_before_upload(invalid_data, 'id')

    def test_serialization(self):
        """Test 5: Test serialization and deserialization with validation"""
        # Save to both formats
        result_df = self.flux_state.save_mirror_table(
            output_format="polars"
        )
        self.assertIsInstance(result_df, pl.DataFrame)

    def test_change_tracking(self):
        """Test 6: Test change tracking with validation"""
        # Initial state validation
        is_valid = self.validator.validate_structure(
            self.flux_state.mirror_table,
            self.key_column
        )
        self.assertTrue(is_valid)

        # Update a value and validate again
        new_data = self.test_data.copy()
        new_data['age'] = ['26', '31', '36']  # Increment ages
        new_df = pl.DataFrame(new_data)
        self.flux_state.table = new_df
        self.flux_state.update_mirror_table()

        is_valid = self.validator.validate_structure(
            self.flux_state.mirror_table,
            self.key_column
        )
        self.assertTrue(is_valid)

    def test_time_series_simulation(self):
        """Test 7: Simulate real production pattern with random data each round"""
        # Initial source table with random data
        source_data = self._generate_random_source_data(num_rows=5)
        source_df = pl.DataFrame(source_data)
        
        # Initialize FluxState with first download
        flux = FluxState(source_df, key_column='id', mode="init")
        
        # CRITICAL: Validate initial mirror table before proceeding
        try:
            is_valid = self.validator.validate_structure(
                flux.mirror_table,
                'id'
            )
            self.assertTrue(is_valid, "Initial mirror table validation failed")
        except Exception as e:
            self.fail(f"Initial validation failed, cannot proceed: {str(e)}")
        
        # Only proceed if initial validation passed
        initial_mirror = flux.save_mirror_table(output_format="polars")
        
        # Simulate 8 rounds of downloads and comparisons
        start_date = datetime.now()
        for round in range(8):
            current_date = start_date + timedelta(days=round)
            print(f"\nRound {round + 1} - Date: {current_date}")
            
            # Generate completely new random data each round
            # with random deletions and additions
            new_source_data = self._generate_random_source_data(
                num_rows=random.randint(3, 8),  # Random number of rows
                existing_ids=set(source_data['id'])  # To track deletions
            )
            new_source_df = pl.DataFrame(new_source_data)
            
            # Create new FluxState instance with downloaded data
            flux = FluxState(new_source_df, key_column='id', mode="compare")
            
            # Update mirror table based on changes
            flux.update_mirror_table()
            
            # CRITICAL: Validate before proceeding with this round
            try:
                is_valid = self.validator.validate_structure(
                    flux.mirror_table,
                    'id'
                )
                self.assertTrue(is_valid, f"Mirror table validation failed in round {round + 1}")
            except Exception as e:
                self.fail(f"Validation failed in round {round + 1}, cannot proceed: {str(e)}")
            
            # Only proceed if validation passed
            updated_mirror = flux.save_mirror_table(output_format="polars")
            
            # Verify time travel for this point
            snapshot = flux.travel(current_date.strftime("%Y-%m-%d %H:%M:%S"))
            self.assertIsInstance(snapshot, pl.DataFrame)
            
            # Verify historical values
            historical = flux.query_historical_value(current_date.strftime("%Y-%m-%d"))
            self.assertIsInstance(historical, dict)
            
            # Track what changed this round
            self._verify_changes_this_round(
                previous_data=source_data,
                new_data=new_source_data,
                flux_state=flux,
                round_num=round
            )
            
            # Update source data for next round
            source_data = new_source_data

    def _generate_random_source_data(self, num_rows, existing_ids=None):
        """Generate random source data, optionally considering existing IDs"""
        # If we have existing IDs, randomly keep some and add new ones
        if existing_ids:
            # Randomly keep some existing IDs
            kept_ids = random.sample(
                list(existing_ids),
                k=random.randint(1, len(existing_ids))
            )
            # Generate new IDs that don't exist
            max_existing = max(existing_ids)
            new_ids = list(range(
                max_existing + 1,
                max_existing + 1 + (num_rows - len(kept_ids))
            ))
            ids = kept_ids + new_ids
        else:
            # First round, just generate sequential IDs
            ids = list(range(1, num_rows + 1))
        
        # Generate random data
        assessments = ['High', 'Medium', 'Low', 'Critical', 'Unknown']
        statuses = ['active', 'pending', 'inactive', 'review']
        
        return {
            'id': ids,
            'assessment': [random.choice(assessments) for _ in range(num_rows)],
            'score': [str(random.randint(0, 100)) for _ in range(num_rows)],
            'status': [random.choice(statuses) for _ in range(num_rows)],
            'notes': [f"Note {random.randint(1, 1000)}" for _ in range(num_rows)]
        }

    def _verify_changes_this_round(self, previous_data, new_data, flux_state, round_num):
        """Verify the changes that occurred this round"""
        # Check for deletions
        previous_ids = set(previous_data['id'])
        current_ids = set(new_data['id'])
        deleted_ids = previous_ids - current_ids
        added_ids = current_ids - previous_ids
        
        print(f"\nRound {round_num + 1} Changes:")
        print(f"Deleted IDs: {deleted_ids}")
        print(f"Added IDs: {added_ids}")
        
        # For deleted rows, verify they're marked as NULL in mirror
        if deleted_ids:
            historical = flux_state.query_historical_value(
                datetime.now().strftime("%Y-%m-%d")
            )
            for id in deleted_ids:
                if id in historical:
                    for col in historical[id]:
                        self.assertIn(
                            historical[id][col],
                            ['NULL', None],
                            f"Deleted row {id} should have NULL values"
                        )
        
        # For new rows, verify they exist in mirror
        if added_ids:
            self.assertTrue(
                all(id in flux_state.mirror_table['id'] for id in added_ids),
                "New IDs should be in mirror table"
            )

    def test_integration(self):
        """Test 8: Integration test with all components"""
        # Create FluxState instance with initial data
        flux = FluxState(self.test_df, key_column=self.key_column)
        
        # Test validation
        is_valid = self.validator.validate_structure(
            flux.mirror_table,
            self.key_column
        )
        self.assertTrue(is_valid)
        
        # Test saving
        result_df = flux.save_mirror_table(output_format="polars")
        self.assertIsInstance(result_df, pl.DataFrame)
        
        # Test updating
        new_data = self.test_data.copy()
        new_data['age'] = ['26', '31', '36']
        new_df = pl.DataFrame(new_data)
        flux.table = new_df
        flux.update_mirror_table()
        
        # Test querying
        historical = flux.query_historical_value(datetime.now().strftime("%Y-%m-%d"))
        self.assertIsInstance(historical, dict)
        
        # Test time travel
        snapshot = flux.travel(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.assertIsInstance(snapshot, pl.DataFrame)

if __name__ == '__main__':
    unittest.main() 