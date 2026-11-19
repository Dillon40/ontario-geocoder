"""
Base classes for the transform pipeline.

Defines core abstractions for record transformation:
- TransformResult: Outcome enum for transformations
- TransformContext: Holds record state, modifications, errors, warnings
- BaseTransformer: Abstract base for synchronous transformers
- AsyncTransformer: Abstract base for async transformers (DB lookups)
- SetupTransformer: Abstract base for one-time setup transformers
- SetupResult: Result of a setup transformer operation
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class TransformResult(Enum):
    """Outcome of a transformation step."""
    SUCCESS = "success"      # No changes needed, record valid
    MODIFIED = "modified"    # Record was modified
    SKIPPED = "skipped"      # Record skipped (e.g., validation failed, non-fatal)
    ERROR = "error"          # Fatal error, record cannot be processed


@dataclass
class TransformModification:
    """Records a single field modification."""
    transformer: str         # Name of transformer that made the change
    field: str               # Field that was modified
    original_value: Any      # Value before modification
    new_value: Any           # Value after modification
    reason: Optional[str] = None  # Optional explanation


@dataclass
class TransformWarning:
    """A non-fatal warning from transformation."""
    transformer: str
    message: str
    field: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


@dataclass
class TransformError:
    """A transformation error; only explicit data defects may be quarantined."""
    transformer: str
    message: str
    field: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    data_error: bool = False


@dataclass
class TransformContext:
    """
    Holds the state of a record being transformed.

    Tracks the record data, modifications made, warnings, and errors.
    Provides methods to modify fields with automatic tracking.
    """
    record: Dict[str, Any]
    source_name: str
    row_number: Optional[int] = None

    # Tracking
    modifications: List[TransformModification] = field(default_factory=list)
    warnings: List[TransformWarning] = field(default_factory=list)
    errors: List[TransformError] = field(default_factory=list)

    # Original values preserved for reference
    original_values: Dict[str, Any] = field(default_factory=dict)

    # Set of fields that have been modified
    modified_fields: Set[str] = field(default_factory=set)

    # Result of last transformation
    last_result: TransformResult = TransformResult.SUCCESS

    @property
    def has_errors(self) -> bool:
        """Check if any errors occurred."""
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        """Check if any warnings occurred."""
        return len(self.warnings) > 0

    @property
    def was_modified(self) -> bool:
        """Check if record was modified."""
        return len(self.modifications) > 0

    def get_field(self, field_name: str, default: Any = None) -> Any:
        """Get a field value from the record."""
        return self.record.get(field_name, default)

    def set_field(
        self,
        field_name: str,
        value: Any,
        transformer: str,
        reason: Optional[str] = None,
        preserve_original: bool = True
    ) -> None:
        """
        Set a field value with modification tracking.

        Args:
            field_name: Name of the field to set
            value: New value
            transformer: Name of transformer making the change
            reason: Optional explanation for the change
            preserve_original: If True and field not previously modified,
                               save original value
        """
        original = self.record.get(field_name)

        # Only track if value actually changed
        if original != value:
            # Preserve original value (first modification only)
            if preserve_original and field_name not in self.original_values:
                self.original_values[field_name] = original

            # Record modification
            self.modifications.append(TransformModification(
                transformer=transformer,
                field=field_name,
                original_value=original,
                new_value=value,
                reason=reason
            ))

            self.modified_fields.add(field_name)
            self.last_result = TransformResult.MODIFIED

        # Set the value
        self.record[field_name] = value

    def add_warning(
        self,
        transformer: str,
        message: str,
        field: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add a non-fatal warning."""
        self.warnings.append(TransformWarning(
            transformer=transformer,
            message=message,
            field=field,
            details=details
        ))

    def add_error(
        self,
        transformer: str,
        message: str,
        field: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        *,
        data_error: bool = False
    ) -> None:
        """Add an error; unexpected failures are never data quarantines."""
        self.errors.append(TransformError(
            transformer=transformer,
            message=message,
            field=field,
            details=details,
            data_error=data_error
        ))
        self.last_result = TransformResult.ERROR

    def mark_skipped(self, transformer: str, reason: str) -> None:
        """Mark record as skipped."""
        self.add_warning(transformer, f"Skipped: {reason}")
        self.last_result = TransformResult.SKIPPED

    def get_original(self, field_name: str) -> Any:
        """Get original value of a field (before any modifications)."""
        return self.original_values.get(field_name, self.record.get(field_name))

    def get_modification_summary(self) -> Dict[str, List[str]]:
        """Get summary of modifications grouped by transformer."""
        summary: Dict[str, List[str]] = {}
        for mod in self.modifications:
            if mod.transformer not in summary:
                summary[mod.transformer] = []
            summary[mod.transformer].append(
                f"{mod.field}: {repr(mod.original_value)} → {repr(mod.new_value)}"
            )
        return summary


class BaseTransformer(ABC):
    """
    Abstract base class for synchronous transformers.

    Transformers modify records in a TransformContext. Each transformer
    should focus on a single responsibility (parsing, normalizing, etc.).
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize transformer with optional config.

        Args:
            config: Transformer-specific configuration
        """
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this transformer."""
        pass

    @abstractmethod
    def transform(self, context: TransformContext) -> TransformResult:
        """
        Transform the record in context.

        Args:
            context: TransformContext containing record to transform

        Returns:
            TransformResult indicating outcome
        """
        pass

    def should_run(self, context: TransformContext) -> bool:
        """
        Check if transformer should run for this context.

        Override to add custom skip logic.

        Args:
            context: TransformContext to check

        Returns:
            True if transformer should run
        """
        return self.enabled and not context.has_errors


class AsyncTransformer(BaseTransformer):
    """
    Abstract base class for async transformers.

    Use for transformers that need database access or other async operations.
    Provides both sync and async transform methods.
    """

    @abstractmethod
    async def transform_async(self, context: TransformContext) -> TransformResult:
        """
        Async transform the record in context.

        Args:
            context: TransformContext containing record to transform

        Returns:
            TransformResult indicating outcome
        """
        pass

    def transform(self, context: TransformContext) -> TransformResult:
        """
        Sync transform - raises error for async transformers.

        Use transform_async() instead, or run in event loop.
        """
        raise NotImplementedError(
            f"{self.name} is an async transformer. "
            "Use transform_async() or run pipeline with async methods."
        )

    async def transform_batch_async(
        self,
        contexts: List[TransformContext],
        batch_size: int = 100
    ) -> List[TransformResult]:
        """
        Transform multiple contexts efficiently.

        Override to implement batch processing (e.g., batch DB queries).
        Default implementation processes one at a time.

        Args:
            contexts: List of TransformContexts
            batch_size: Size of batches for processing

        Returns:
            List of TransformResults
        """
        results = []
        for context in contexts:
            result = await self.transform_async(context)
            results.append(result)
        return results


@dataclass
class SetupResult:
    """
    Result of a setup transformer operation.

    Tracks outcome, timing, and details of reference table population.
    """
    status: str  # 'success', 'skipped', 'error'
    reason: Optional[str] = None
    rows_affected: int = 0
    duration_seconds: float = 0.0
    details: Optional[Dict[str, Any]] = None


class SetupTransformer(ABC):
    """
    Abstract base class for one-time setup transformers.

    Setup transformers run during pipeline initialization to populate
    reference tables (like municipality_aliases, fsa_municipality_mappings).

    Unlike per-record transformers, setup transformers:
    - Run once per pipeline initialization (not per record)
    - Can perform bulk database operations
    - Check if setup is already complete to avoid redundant work

    Usage:
        class MySetup(SetupTransformer):
            @property
            def name(self) -> str:
                return "MySetup"

            async def needs_setup(self, pool) -> bool:
                # Check if table is empty
                async with pool.acquire() as conn:
                    count = await conn.fetchval("SELECT COUNT(*) FROM my_table")
                    return count == 0

            async def run_setup(self, pool) -> SetupResult:
                # Populate reference table
                ...
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize setup transformer.

        Args:
            config: Optional configuration dict
        """
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this setup transformer."""
        pass

    @abstractmethod
    async def needs_setup(self, pool) -> bool:
        """
        Check if setup needs to run.

        Returns True if reference tables are empty or stale.
        Should be fast (just check row counts).

        Args:
            pool: Database connection pool

        Returns:
            True if setup should run
        """
        pass

    async def check_dependencies(self, pool) -> Tuple[bool, str]:
        """
        Check if required dependencies are available.

        Override to verify upstream data exists before running setup.

        Args:
            pool: Database connection pool

        Returns:
            Tuple of (dependencies_ok, error_message)
        """
        return True, ""

    @abstractmethod
    async def run_setup(self, pool) -> SetupResult:
        """
        Perform the setup operation.

        Populates reference tables needed by per-record transformers.

        Args:
            pool: Database connection pool

        Returns:
            SetupResult with status and statistics
        """
        pass

    async def ensure_setup(self, pool, force: bool = False) -> SetupResult:
        """
        Ensure setup is complete, running if needed.

        Args:
            pool: Database connection pool
            force: If True, run setup even if tables are already populated

        Returns:
            SetupResult with status and statistics
        """
        import time

        if not self.enabled:
            return SetupResult(status='skipped', reason='Disabled in config')

        # Check dependencies first
        deps_ok, deps_msg = await self.check_dependencies(pool)
        if not deps_ok:
            return SetupResult(status='error', reason=deps_msg)

        # Check if setup is needed
        if not force and not await self.needs_setup(pool):
            return SetupResult(status='skipped', reason='Already populated')

        # Run setup
        start = time.time()
        try:
            result = await self.run_setup(pool)
            result.duration_seconds = time.time() - start
            return result
        except Exception as e:
            return SetupResult(
                status='error',
                reason=str(e),
                duration_seconds=time.time() - start
            )
