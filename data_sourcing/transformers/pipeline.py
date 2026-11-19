"""
Transform pipeline orchestrator.

Orchestrates the execution of transformers in sequence, handling both
synchronous and asynchronous transformers. Provides batch processing
capabilities for efficient database operations.

Also manages setup transformers that run once during pipeline initialization
to populate reference tables (municipality aliases, FSA-municipality mappings).
"""
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

from .base import (
    AsyncTransformer,
    BaseTransformer,
    SetupResult,
    TransformContext,
    TransformResult,
)
from .config_loader import TransformConfigLoader


@dataclass
class PipelineStatistics:
    """Statistics collected during pipeline execution."""
    total_records: int = 0
    successful_records: int = 0
    modified_records: int = 0
    skipped_records: int = 0
    error_records: int = 0

    # Per-transformer statistics
    transformer_counts: Dict[str, int] = field(default_factory=dict)

    def record_result(self, result: TransformResult, context: TransformContext):
        """Record result of a single transformation."""
        self.total_records += 1

        if result == TransformResult.ERROR or context.has_errors:
            self.error_records += 1
        elif result == TransformResult.SKIPPED:
            self.skipped_records += 1
        elif context.was_modified:
            self.modified_records += 1
            self.successful_records += 1
        else:
            self.successful_records += 1

        # Count transformer modifications
        for mod in context.modifications:
            transformer = mod.transformer
            self.transformer_counts[transformer] = self.transformer_counts.get(transformer, 0) + 1

    def summary(self) -> str:
        """Get human-readable summary."""
        lines = [
            f"Total records: {self.total_records}",
            f"Successful: {self.successful_records}",
            f"Modified: {self.modified_records}",
            f"Skipped: {self.skipped_records}",
            f"Errors: {self.error_records}",
        ]

        if self.transformer_counts:
            lines.append("Transformations by type:")
            for transformer, count in sorted(self.transformer_counts.items()):
                lines.append(f"  - {transformer}: {count}")

        return '\n'.join(lines)


class TransformPipeline:
    """
    Orchestrates transform execution for address records.

    Supports both synchronous and asynchronous execution modes.

    Usage (sync):
        pipeline = TransformPipeline.for_source('guelph')
        context = pipeline.transform(record)

    Usage (async):
        pipeline = await TransformPipeline.for_source_async('toronto', pool)
        context = await pipeline.transform_async(record)
    """

    def __init__(
        self,
        source_name: str,
        transformers: Optional[List[BaseTransformer]] = None
    ):
        """
        Initialize pipeline.

        Args:
            source_name: Name of the data source
            transformers: List of transformer instances
        """
        self.source_name = source_name
        self.transformers = transformers or []
        self.statistics = PipelineStatistics()

    @classmethod
    def for_source(
        cls,
        source_name: str,
        aliases: Optional[Dict[str, str]] = None
    ) -> 'TransformPipeline':
        """
        Create sync pipeline for a source.

        Args:
            source_name: Name of the source
            aliases: Municipality aliases for sync mapper

        Returns:
            TransformPipeline instance
        """
        loader = TransformConfigLoader(source_name)
        transformers = loader.build_transformers_sync(aliases)
        return cls(source_name, transformers)

    @classmethod
    async def for_source_async(
        cls,
        source_name: str,
        pool,
        skip_setup: bool = False
    ) -> 'TransformPipeline':
        """
        Create async pipeline for a source with database access.

        Args:
            source_name: Name of the source
            pool: Database connection pool
            skip_setup: If True, skip setup transformers (faster when data exists)

        Returns:
            TransformPipeline instance with async transformers
        """
        # Phase 1: Run setup transformers (populate reference tables)
        if not skip_setup:
            setup_results = await cls._run_setup_transformers(pool)
            for result in setup_results:
                if result.status == 'error':
                    raise RuntimeError(f"Transform setup failed: {result.reason}")

        # Phase 2: Build per-record transformers
        loader = TransformConfigLoader(source_name)
        transformers = await loader.build_transformers_async(pool)
        return cls(source_name, transformers)

    @classmethod
    async def _run_setup_transformers(cls, pool) -> List[SetupResult]:
        """
        Run all setup transformers to populate reference tables.

        Setup transformers are idempotent - they check if tables are already
        populated and skip if so.

        Args:
            pool: Database connection pool

        Returns:
            List of SetupResult from each setup transformer
        """
        from .municipality_alias_setup import MunicipalityAliasSetup
        from .fsa_municipality_setup import FSAMunicipalitySetup

        setup_transformers = [
            MunicipalityAliasSetup(),
            FSAMunicipalitySetup(),
        ]

        results = []
        for setup in setup_transformers:
            result = await setup.ensure_setup(pool)
            results.append(result)

            if result.status == 'success':
                print(f"  Setup {setup.name}: {result.rows_affected} rows "
                      f"in {result.duration_seconds:.1f}s")
            elif result.status == 'skipped':
                # Silently skip - already populated
                pass
            elif result.status == 'error':
                print(f"  Warning: {setup.name} failed: {result.reason}")

        return results

    def create_context(
        self,
        record: Dict[str, Any],
        row_number: Optional[int] = None
    ) -> TransformContext:
        """
        Create a TransformContext for a record.

        Args:
            record: Record dict to transform
            row_number: Optional row number for error reporting

        Returns:
            TransformContext instance
        """
        return TransformContext(
            record=record.copy(),  # Copy to avoid modifying original
            source_name=self.source_name,
            row_number=row_number
        )

    def transform(
        self,
        record: Dict[str, Any],
        row_number: Optional[int] = None
    ) -> TransformContext:
        """
        Transform a single record synchronously.

        Args:
            record: Record dict to transform
            row_number: Optional row number for error reporting

        Returns:
            TransformContext with transformed record and metadata
        """
        context = self.create_context(record, row_number)

        for transformer in self.transformers:
            if isinstance(transformer, AsyncTransformer):
                # Skip async transformers in sync mode
                context.add_warning(
                    transformer.name,
                    "Skipped async transformer in sync mode"
                )
                continue

            if not transformer.should_run(context):
                continue

            try:
                transformer.transform(context)
            except Exception as e:
                context.add_error(
                    transformer.name,
                    f"Transform error: {str(e)}"
                )

            # Stop on error
            if context.has_errors:
                break

        self.statistics.record_result(context.last_result, context)
        return context

    async def transform_async(
        self,
        record: Dict[str, Any],
        row_number: Optional[int] = None
    ) -> TransformContext:
        """
        Transform a single record asynchronously.

        Args:
            record: Record dict to transform
            row_number: Optional row number for error reporting

        Returns:
            TransformContext with transformed record and metadata
        """
        context = self.create_context(record, row_number)

        for transformer in self.transformers:
            if not transformer.should_run(context):
                continue

            try:
                if isinstance(transformer, AsyncTransformer):
                    await transformer.transform_async(context)
                else:
                    transformer.transform(context)
            except Exception as e:
                context.add_error(
                    transformer.name,
                    f"Transform error: {str(e)}"
                )

            # Stop on error
            if context.has_errors:
                break

        self.statistics.record_result(context.last_result, context)
        return context

    def transform_batch(
        self,
        records: List[Dict[str, Any]],
        start_row: int = 0
    ) -> Iterator[TransformContext]:
        """
        Transform batch of records synchronously.

        Args:
            records: List of record dicts
            start_row: Starting row number for error reporting

        Yields:
            TransformContext for each record
        """
        for i, record in enumerate(records):
            yield self.transform(record, row_number=start_row + i)

    async def transform_batch_async(
        self,
        records: List[Dict[str, Any]],
        start_row: int = 0,
        batch_size: int = 100
    ) -> AsyncIterator[TransformContext]:
        """
        Transform batch of records asynchronously with batched DB queries.

        For transformers that support batch operations (like FSAEnricher),
        this method can be more efficient than calling transform_async
        on each record individually.

        Args:
            records: List of record dicts
            start_row: Starting row number for error reporting
            batch_size: Size of batches for batch-capable transformers

        Yields:
            TransformContext for each record
        """
        # Create contexts for all records
        contexts = [
            self.create_context(record, row_number=start_row + i)
            for i, record in enumerate(records)
        ]

        # Process each transformer
        for transformer in self.transformers:
            # Separate async batch-capable transformers
            if isinstance(transformer, AsyncTransformer):
                # Use batch method if available
                if hasattr(transformer, 'transform_batch_async'):
                    active_contexts = [
                        ctx for ctx in contexts
                        if transformer.should_run(ctx)
                    ]
                    if active_contexts:
                        try:
                            await transformer.transform_batch_async(
                                active_contexts,
                                batch_size=batch_size
                            )
                        except Exception as e:
                            for ctx in active_contexts:
                                ctx.add_error(
                                    transformer.name,
                                    f"Batch transform error: {str(e)}"
                                )
                else:
                    # Fall back to individual async transforms
                    for ctx in contexts:
                        if transformer.should_run(ctx):
                            try:
                                await transformer.transform_async(ctx)
                            except Exception as e:
                                ctx.add_error(
                                    transformer.name,
                                    f"Transform error: {str(e)}"
                                )
            else:
                # Sync transformer - process each context
                for ctx in contexts:
                    if transformer.should_run(ctx):
                        try:
                            transformer.transform(ctx)
                        except Exception as e:
                            ctx.add_error(
                                transformer.name,
                                f"Transform error: {str(e)}"
                            )

        # Record statistics and yield results
        for ctx in contexts:
            self.statistics.record_result(ctx.last_result, ctx)
            yield ctx

    def get_statistics(self) -> PipelineStatistics:
        """Get pipeline execution statistics."""
        return self.statistics

    def reset_statistics(self) -> None:
        """Reset pipeline statistics."""
        self.statistics = PipelineStatistics()

    def format_dry_run_output(
        self,
        contexts: List[TransformContext],
        max_records: int = 5
    ) -> str:
        """
        Format detailed dry-run output for debugging.

        Args:
            contexts: List of transformed contexts
            max_records: Maximum records to show in detail

        Returns:
            Formatted string for display
        """
        lines = [
            "=" * 80,
            "DRY RUN: TRANSFORM PIPELINE PREVIEW",
            "=" * 80,
            f"Source: {self.source_name}",
            f"Sample Size: {len(contexts)} records",
            "",
        ]

        for i, ctx in enumerate(contexts[:max_records]):
            lines.append(f"Record {i + 1}/{min(len(contexts), max_records)}:")

            # Show input (original values)
            lines.append("  Input:")
            for field, value in ctx.original_values.items():
                lines.append(f"    {field}: {repr(value)}")
            # Also show fields that weren't modified
            for field, value in ctx.record.items():
                if field not in ctx.original_values and field not in ctx.modified_fields:
                    if value is not None:
                        lines.append(f"    {field}: {repr(value)}")

            # Show transformations
            if ctx.modifications:
                lines.append("")
                lines.append("  Transformations Applied:")
                for mod in ctx.modifications:
                    lines.append(
                        f"    [{mod.transformer}] {mod.field}: "
                        f"{repr(mod.original_value)} → {repr(mod.new_value)}"
                    )

            # Show output
            lines.append("")
            lines.append("  Output:")
            for field in sorted(ctx.modified_fields):
                lines.append(f"    {field}: {repr(ctx.record.get(field))}")

            # Show validation result
            lines.append("")
            if ctx.has_errors:
                lines.append(f"  Validation: ✗ FAILED")
                for error in ctx.errors:
                    lines.append(f"    - {error.message}")
            elif ctx.has_warnings:
                lines.append(f"  Validation: ⚠ PASSED with warnings")
                for warning in ctx.warnings:
                    lines.append(f"    - {warning.message}")
            else:
                lines.append(f"  Validation: ✓ PASSED")

            lines.append("")

        # Summary
        lines.append("=" * 80)
        lines.append("DRY RUN SUMMARY")
        lines.append("=" * 80)
        lines.append(f"Records sampled: {len(contexts)}")

        total_transforms = sum(
            len(ctx.modifications) for ctx in contexts
        )
        lines.append(f"Transformations applied: {total_transforms}")

        # Count by transformer
        transformer_counts: Dict[str, int] = {}
        for ctx in contexts:
            for mod in ctx.modifications:
                transformer_counts[mod.transformer] = transformer_counts.get(
                    mod.transformer, 0
                ) + 1

        for transformer, count in sorted(transformer_counts.items()):
            lines.append(f"  - {transformer}: {count}")

        # Validation summary
        passed = sum(1 for ctx in contexts if not ctx.has_errors)
        lines.append(f"Validation passed: {passed}/{len(contexts)}")

        if passed == len(contexts):
            lines.append("Ready for import: YES")
        else:
            lines.append("Ready for import: NO (validation errors)")

        return '\n'.join(lines)
