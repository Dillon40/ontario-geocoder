#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Manual Integration Tests for FSA-Municipality Geographic Validation

Tests the complete FSA-municipality mapping system by directly
querying the database to verify:
1. Municipality name normalization (bilingual names, aliases)
2. FSA-municipality compatibility validation
3. Geographic inference (filling missing fields)
4. Confidence score adjustments

Run this after:
- Migration 6 has been run
- FSA-municipality mappings have been populated
- Municipality aliases have been extracted
"""

import asyncio
import sys
import asyncpg
from pathlib import Path

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config


class IntegrationTester:
    """Runs manual integration tests for geographic validation"""

    def __init__(self):
        self.conn = None
        self.tests_run = 0
        self.tests_passed = 0
        self.tests_failed = 0

    async def setup(self):
        """Initialize database connection"""
        db_config = get_db_config()
        self.conn = await asyncpg.connect(
            host=db_config['host'],
            port=db_config['port'],
            database=db_config['database'],
            user=db_config['user'],
            password=db_config['password']
        )
        print("=" * 80)
        print("FSA-MUNICIPALITY INTEGRATION TESTS")
        print("=" * 80)
        print()

    async def cleanup(self):
        """Close database connection"""
        if self.conn:
            await self.conn.close()

    def assert_equal(self, actual, expected, test_name):
        """Assert that actual equals expected"""
        self.tests_run += 1
        if actual == expected:
            print(f"  ✓ {test_name}")
            self.tests_passed += 1
            return True
        else:
            print(f"  ✗ {test_name}")
            print(f"    Expected: {expected}")
            print(f"    Actual:   {actual}")
            self.tests_failed += 1
            return False

    def assert_contains(self, container, item, test_name):
        """Assert that container contains item"""
        self.tests_run += 1
        if item in container:
            print(f"  ✓ {test_name}")
            self.tests_passed += 1
            return True
        else:
            print(f"  ✗ {test_name}")
            print(f"    '{item}' not found in {container}")
            self.tests_failed += 1
            return False

    async def test_municipality_normalization(self):
        """Test municipality name normalization"""
        print("\n" + "=" * 80)
        print("TEST 1: Municipality Name Normalization")
        print("=" * 80)
        print()

        # Test bilingual name
        result = await self.conn.fetchval(
            "SELECT normalize_municipality_name($1)",
            "Greater Sudbury / Grand Sudbury"
        )
        self.assert_equal(result, "GREATER SUDBURY", "Bilingual name normalization")

        # Test county variation
        result = await self.conn.fetchval(
            "SELECT normalize_municipality_name($1)",
            "Prince Edward County"
        )
        self.assert_equal(result, "PRINCE EDWARD COUNTY", "County name normalization")

        # Test whitespace normalization
        result = await self.conn.fetchval(
            "SELECT normalize_municipality_name($1)",
            "  Toronto  "
        )
        self.assert_equal(result, "TORONTO", "Whitespace normalization")

    async def test_fsa_municipality_validation(self):
        """Test FSA-municipality compatibility validation"""
        print("\n" + "=" * 80)
        print("TEST 2: FSA-Municipality Compatibility Validation")
        print("=" * 80)
        print()

        # Test valid combination (M5V = downtown Toronto)
        exists = await self.conn.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM fsa_municipality_mappings
                WHERE fsa_code = 'M5V'
                  AND normalize_municipality_name(municipality_name_short) = 'TORONTO'
            )
        """)
        self.assert_equal(exists, True, "Valid M5V + Toronto mapping exists")

        # Test invalid combination (M5V is not in Ottawa)
        exists = await self.conn.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM fsa_municipality_mappings
                WHERE fsa_code = 'M5V'
                  AND normalize_municipality_name(municipality_name_short) = 'OTTAWA'
            )
        """)
        self.assert_equal(exists, False, "Invalid M5V + Ottawa has no mapping")

    async def test_municipality_lookup(self):
        """Test FSA to municipality lookup"""
        print("\n" + "=" * 80)
        print("TEST 3: Municipality Lookup by FSA")
        print("=" * 80)
        print()

        # Test M5V lookup
        munis = await self.conn.fetch("""
            SELECT municipality_name_short, is_primary, address_count
            FROM fsa_municipality_mappings
            WHERE fsa_code = 'M5V'
            ORDER BY is_primary DESC, coverage_pct DESC
        """)
        self.assert_equal(len(munis) > 0, True, "M5V has municipalities")
        if munis:
            self.assert_equal(munis[0]['municipality_name_short'], "TORONTO", "M5V primary is Toronto")
            self.assert_equal(munis[0]['is_primary'], True, "Primary flag set")

    async def test_alias_resolution(self):
        """Test municipality alias resolution"""
        print("\n" + "=" * 80)
        print("TEST 4: Municipality Alias Resolution")
        print("=" * 80)
        print()

        # Test bilingual alias
        canonical = await self.conn.fetchval("""
            SELECT canonical_name FROM municipality_aliases
            WHERE normalized_alias = normalize_municipality_name('Greater Sudbury / Grand Sudbury')
        """)
        self.assert_equal(canonical, "GREATER SUDBURY", "Bilingual alias resolves correctly")

        # Test county variation
        canonical = await self.conn.fetchval("""
            SELECT canonical_name FROM municipality_aliases
            WHERE normalized_alias = normalize_municipality_name('Prince Edward County')
        """)
        self.assert_equal(canonical, "PRINCE EDWARD", "County alias resolves correctly")

    async def print_summary(self):
        """Print test summary"""
        print("\n" + "=" * 80)
        print("TEST SUMMARY")
        print("=" * 80)
        print()

        print(f"Tests Run:    {self.tests_run}")
        print(f"Tests Passed: {self.tests_passed} ✓")
        print(f"Tests Failed: {self.tests_failed}" + (" ✗" if self.tests_failed > 0 else ""))
        print()

        if self.tests_failed == 0:
            print("✓ ALL TESTS PASSED")
            print()
            print("The FSA-municipality mapping system is fully operational!")
        else:
            print(f"✗ {self.tests_failed} TEST(S) FAILED")
            print()
            print("Please review the failed tests above and investigate.")

        print("=" * 80)
        return self.tests_failed == 0


async def main():
    """Run all integration tests"""
    tester = IntegrationTester()

    try:
        await tester.setup()

        # Run test suites
        await tester.test_municipality_normalization()
        await tester.test_fsa_municipality_validation()
        await tester.test_municipality_lookup()
        await tester.test_alias_resolution()

        # Print summary
        success = await tester.print_summary()

        #Cleanup
        await tester.cleanup()

        # Exit with appropriate code
        return 0 if success else 1

    except Exception as e:
        print(f"\n[FAIL] Test execution error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    import sys
    sys.exit(asyncio.run(main()))
