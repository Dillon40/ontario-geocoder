"""
Transform pipeline for address data processing.

This package provides a modular transform pipeline for processing address
records before loading into the database. It handles:
- Street name parsing (composite street names → components)
- Field normalization (suffix, direction, postal code, province)
- Municipality mapping (pre-amalgamation → canonical names)
- FSA enrichment (spatial lookup for missing postal codes)
- Record validation (required fields, coordinate bounds)
- Setup transformers (one-time reference table population)

Usage (sync for testing):
    from data_sourcing.transformers import TransformPipeline

    pipeline = TransformPipeline.for_source('guelph')
    context = pipeline.transform(record)
    if context.has_errors:
        print("Validation failed:", context.errors)
    else:
        transformed_record = context.record

Usage (async for production):
    from data_sourcing.transformers import TransformPipeline

    pipeline = await TransformPipeline.for_source_async('toronto', db_pool)
    context = await pipeline.transform_async(record)

    # Skip setup transformers if reference tables already populated
    pipeline = await TransformPipeline.for_source_async('toronto', db_pool, skip_setup=True)
"""

from .base import (
    TransformResult,
    TransformContext,
    TransformModification,
    TransformWarning,
    TransformError,
    BaseTransformer,
    AsyncTransformer,
    SetupTransformer,
    SetupResult,
)
from .street_name_parser import StreetNameParser, parse_street_name
from .civic_number_parser import CivicNumberParser, parse_civic_number
from .unit_number_parser import UnitNumberParser, parse_unit_number, strip_unit_label
from .field_normalizer import (
    FieldNormalizer,
    normalize_street_suffix,
    normalize_direction,
    normalize_postal_code,
    normalize_province,
    normalize_city,
    normalize_unit_number,
    normalize_civic_suffix,
)
from .municipality_mapper import (
    MunicipalityMapper,
    MunicipalityMapperSync,
)
from .fsa_enricher import FSAEnricher
from .record_validator import (
    RecordValidator,
    CoordinateValidator,
    PostalCodeValidator,
    validate_postal_code,
    validate_coordinates,
)
from .pipeline import TransformPipeline, PipelineStatistics
from .municipality_alias_setup import MunicipalityAliasSetup
from .fsa_municipality_setup import FSAMunicipalitySetup
from .postal_municipality_setup import PostalMunicipalitySetup
from .config_loader import (
    TransformConfigLoader,
    load_dataset_mappings,
    get_source_config,
    get_transform_config,
)


__all__ = [
    # Base classes
    'TransformResult',
    'TransformContext',
    'TransformModification',
    'TransformWarning',
    'TransformError',
    'BaseTransformer',
    'AsyncTransformer',
    'SetupTransformer',
    'SetupResult',

    # Per-record transformers
    'StreetNameParser',
    'CivicNumberParser',
    'UnitNumberParser',
    'FieldNormalizer',
    'MunicipalityMapper',
    'MunicipalityMapperSync',
    'FSAEnricher',
    'RecordValidator',
    'CoordinateValidator',
    'PostalCodeValidator',

    # Setup transformers (one-time reference table population)
    'MunicipalityAliasSetup',
    'FSAMunicipalitySetup',
    'PostalMunicipalitySetup',

    # Pipeline
    'TransformPipeline',
    'PipelineStatistics',

    # Config
    'TransformConfigLoader',
    'load_dataset_mappings',
    'get_source_config',
    'get_transform_config',

    # Utility functions
    'parse_street_name',
    'parse_civic_number',
    'parse_unit_number',
    'strip_unit_label',
    'normalize_street_suffix',
    'normalize_direction',
    'normalize_postal_code',
    'normalize_province',
    'normalize_city',
    'normalize_unit_number',
    'normalize_civic_suffix',
    'validate_postal_code',
    'validate_coordinates',
]
