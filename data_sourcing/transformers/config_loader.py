"""
Configuration loader for transform pipeline.

Loads transform configuration from dataset-mappings.json and builds
transformer instances for a given source.
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional

from .street_name_parser import StreetNameParser
from .civic_number_parser import CivicNumberParser
from .unit_number_parser import UnitNumberParser
from .field_normalizer import FieldNormalizer
from .municipality_mapper import MunicipalityMapper, MunicipalityMapperSync
from .fsa_enricher import FSAEnricher
from .record_validator import RecordValidator


# Load dataset mappings
config_dir = Path(__file__).parent.parent.parent / 'config'


def load_dataset_mappings() -> Dict[str, Any]:
    """Load dataset-mappings.json configuration."""
    with open(config_dir / 'dataset-mappings.json', 'r') as f:
        return json.load(f)


def get_source_config(source_name: str) -> Optional[Dict[str, Any]]:
    """
    Get configuration for a specific source.

    Args:
        source_name: Name of the source (e.g., 'ottawa', 'toronto', 'guelph')

    Returns:
        Source configuration dict or None if not found
    """
    mappings = load_dataset_mappings()
    return mappings.get(source_name)


def get_transform_config(source_name: str) -> Dict[str, Any]:
    """
    Get transform configuration for a source.

    Args:
        source_name: Name of the source

    Returns:
        Transform configuration dict (may be empty if not defined)
    """
    source_config = get_source_config(source_name)
    if not source_config:
        return {}
    return source_config.get('transforms', {})


class TransformConfigLoader:
    """
    Loads transform configuration and builds transformer instances.

    Usage:
        loader = TransformConfigLoader('ottawa')
        transformers = loader.build_transformers()
    """

    def __init__(self, source_name: str):
        """
        Initialize loader for a source.

        Args:
            source_name: Name of the source
        """
        self.source_name = source_name
        self.source_config = get_source_config(source_name) or {}
        self.transform_config = self.source_config.get('transforms', {})

    def get_config(self, transform_name: str) -> Dict[str, Any]:
        """Get configuration for a specific transformer."""
        return self.transform_config.get(transform_name, {})

    def is_enabled(self, transform_name: str) -> bool:
        """Check if a transformer is enabled."""
        config = self.get_config(transform_name)
        return config.get('enabled', False)

    def build_street_name_parser(self) -> Optional[StreetNameParser]:
        """Build StreetNameParser if enabled."""
        config = self.get_config('parse_street_name')
        if not config.get('enabled', False):
            return None
        return StreetNameParser(config)

    def build_civic_number_parser(self) -> Optional[CivicNumberParser]:
        """Build CivicNumberParser if enabled."""
        config = self.get_config('parse_civic_number')
        if not config.get('enabled', True):  # Enabled by default
            return None
        return CivicNumberParser(config)

    def build_unit_number_parser(self) -> Optional[UnitNumberParser]:
        """Build UnitNumberParser if enabled."""
        config = self.get_config('parse_unit_number')
        if not config.get('enabled', False):  # Disabled by default
            return None
        return UnitNumberParser(config)

    def build_field_normalizer(self) -> Optional[FieldNormalizer]:
        """Build FieldNormalizer if enabled."""
        config = self.get_config('normalize_fields')
        if not config.get('enabled', True):  # Enabled by default
            return None
        return FieldNormalizer(config)

    def build_municipality_mapper_sync(
        self,
        aliases: Optional[Dict[str, str]] = None
    ) -> Optional[MunicipalityMapperSync]:
        """
        Build sync MunicipalityMapper for testing.

        Args:
            aliases: Pre-loaded aliases dict

        Returns:
            MunicipalityMapperSync instance or None
        """
        config = self.get_config('map_municipality')
        if not config.get('enabled', False):
            return None
        return MunicipalityMapperSync(aliases=aliases, config=config)

    def build_municipality_mapper(self, pool=None) -> Optional[MunicipalityMapper]:
        """
        Build async MunicipalityMapper.

        Note: Call load_aliases() before using.

        Args:
            pool: Database connection pool (needed for derive_from_coordinates)

        Returns:
            MunicipalityMapper instance or None
        """
        config = self.get_config('map_municipality')
        if not config.get('enabled', False):
            return None
        return MunicipalityMapper(config, pool=pool)

    def build_fsa_enricher(self, pool=None) -> Optional[FSAEnricher]:
        """
        Build FSAEnricher if enabled.

        Args:
            pool: Database connection pool

        Returns:
            FSAEnricher instance or None
        """
        config = self.get_config('enrich_fsa')
        if not config.get('enabled', False):
            return None
        return FSAEnricher(config, pool=pool)

    def build_record_validator(self) -> Optional[RecordValidator]:
        """Build RecordValidator if enabled."""
        config = self.get_config('validate')
        # Validator is typically always enabled
        enabled = config.get('enabled', True) if config else True
        if not enabled:
            return None
        return RecordValidator(config)

    def build_transformers_sync(
        self,
        aliases: Optional[Dict[str, str]] = None
    ) -> list:
        """
        Build list of sync transformers for this source.

        For testing without database. Does not include FSAEnricher.

        Args:
            aliases: Municipality aliases for MunicipalityMapperSync

        Returns:
            List of transformer instances
        """
        transformers = []

        # Order matters!
        # 1. Street name parser
        parser = self.build_street_name_parser()
        if parser:
            transformers.append(parser)

        # 2. Civic number parser (extracts suffixes from street numbers)
        civic_parser = self.build_civic_number_parser()
        if civic_parser:
            transformers.append(civic_parser)

        # 3. Unit number parser (extracts unit numbers from address fields)
        unit_parser = self.build_unit_number_parser()
        if unit_parser:
            transformers.append(unit_parser)

        # 4. Field normalizer
        normalizer = self.build_field_normalizer()
        if normalizer:
            transformers.append(normalizer)

        # 5. Municipality mapper (sync version)
        mapper = self.build_municipality_mapper_sync(aliases)
        if mapper:
            transformers.append(mapper)

        # 6. FSA enricher - skip for sync (requires DB)

        # 7. Record validator (last)
        validator = self.build_record_validator()
        if validator:
            transformers.append(validator)

        return transformers

    async def build_transformers_async(self, pool) -> list:
        """
        Build list of async transformers for this source.

        Args:
            pool: Database connection pool

        Returns:
            List of transformer instances (including async ones)
        """
        transformers = []

        # Order matters!
        # 1. Street name parser
        parser = self.build_street_name_parser()
        if parser:
            transformers.append(parser)

        # 2. Civic number parser (extracts suffixes from street numbers)
        civic_parser = self.build_civic_number_parser()
        if civic_parser:
            transformers.append(civic_parser)

        # 3. Unit number parser (extracts unit numbers from address fields)
        unit_parser = self.build_unit_number_parser()
        if unit_parser:
            transformers.append(unit_parser)

        # 4. Field normalizer
        normalizer = self.build_field_normalizer()
        if normalizer:
            transformers.append(normalizer)

        # 5. Municipality mapper (async version with pool for spatial lookups)
        mapper = self.build_municipality_mapper(pool)
        if mapper:
            await mapper.load_aliases(pool)
            transformers.append(mapper)

        # 6. FSA enricher
        enricher = self.build_fsa_enricher(pool)
        if enricher:
            transformers.append(enricher)

        # 7. Record validator (last)
        validator = self.build_record_validator()
        if validator:
            transformers.append(validator)

        return transformers
