"""
Pydantic models for request and response validation.
"""
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, ConfigDict, Field


class IDXAddress(BaseModel):
    """IDX structured address format."""
    StreetNumber: Optional[str] = None
    StreetName: Optional[str] = None
    StreetSuffix: Optional[str] = None
    StreetDirection: Optional[str] = None
    UnitNumber: Optional[str] = None
    City: Optional[str] = None
    Province: Optional[str] = None
    StateOrProvince: Optional[str] = None
    PostalCode: Optional[str] = None
    Country: Optional[str] = "Canada"


class GeocodeRequest(BaseModel):
    """Single address geocode request."""
    # Support multiple input formats
    Address: Optional[IDXAddress] = None
    address: Optional[str] = None
    street_number: Optional[str] = None
    streetNumber: Optional[str] = None
    street_name: Optional[str] = None
    streetName: Optional[str] = None
    street_suffix: Optional[str] = None
    streetSuffix: Optional[str] = None
    street_direction: Optional[str] = None
    streetDirection: Optional[str] = None
    unit_no: Optional[str] = None
    unitNumber: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    postalCode: Optional[str] = None
    pin: Optional[str] = None
    PIN: Optional[str] = None
    mls_number: Optional[str] = None  # MLS listing number (echoed back in response)

    # Reject unknown/typo'd fields (e.g. "streat_name") with a 422 instead of
    # silently ignoring them. All field spellings the IDX site sends (snake_case,
    # camelCase, and the nested IDX "Address" format) are declared above.
    model_config = ConfigDict(extra="forbid")


class ConfidenceFactors(BaseModel):
    """Matcher base (after fuzzy scaling), geography adjustment and tier clamp."""
    base_confidence: float
    tier: int
    match_method: str
    geography_adjustment: float = 0.0
    unclamped_confidence: Optional[float] = None
    confidence_cap: Optional[float] = None
    final_confidence: Optional[float] = None
    clamped: bool = False


class LicenseInfo(BaseModel):
    """License information for a data source."""
    license_type: Optional[str] = None
    license_url: Optional[str] = None
    commercial_use_allowed: Optional[bool] = None
    required_attribution: Optional[str] = None


class GeocodeResponse(BaseModel):
    """Geocode response."""
    # extra="allow" lets fields the engine adds later (e.g. per-result
    # attribution) pass through instead of being stripped by response_model.
    model_config = ConfigDict(extra="allow")

    success: Optional[bool] = True
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    matched_address: Optional[str] = None
    source: Optional[str] = None
    confidence: Optional[float] = None
    match_tier: Optional[int] = None
    # Identify the matching strategy, not the positional accuracy of the
    # source coordinates. An exact address match is not a rooftop guarantee.
    match_method: Optional[str] = None
    match_layer: Optional[int] = None
    fuzzy_match: Optional[bool] = None
    street_similarity: Optional[float] = None
    property_id: Optional[str] = None
    assessment_roll_number: Optional[str] = None
    pin: Optional[str] = None
    gpid: Optional[str] = None
    confidence_factors: Optional[ConfidenceFactors] = None
    cached: Optional[bool] = False
    response_time_ms: Optional[float] = None
    error: Optional[str] = None
    input_address: Optional[str] = None
    # Enhanced response fields
    original_input: Optional[Union[str, Dict[str, Any]]] = None  # Verbatim raw input as received
    mls_number: Optional[str] = None  # MLS listing number (echoed from request)
    license_info: Optional[LicenseInfo] = None  # License info for the data source
    attribution: Optional[str] = None  # Required attribution statement for the matched source


class BatchGeocodeRequest(BaseModel):
    """Batch geocode request."""
    addresses: List[Any] = Field(..., min_length=1, max_length=1000)


class BatchSummary(BaseModel):
    """Batch processing summary."""
    total: int
    successful: int
    failed: int
    cache_hits: int


class BatchGeocodeResponse(BaseModel):
    """Batch geocode response."""
    results: List[Dict[str, Any]]
    summary: BatchSummary


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    database: str
    redis: str
    timestamp: str


class DatabaseStats(BaseModel):
    """Database statistics."""
    properties_count: int
    cache_entries: int
    datasets: Dict[str, int]
    nar_addresses_count: int
    nar_locations_count: int


class CacheStats(BaseModel):
    """Cache statistics."""
    hit_rate: float
    total_hits: int
    total_misses: int
    connected: bool


class TierDistribution(BaseModel):
    """Geocoding tier distribution."""
    tier_1_pin: int
    tier_2_municipal: int
    tier_3_nar: int


class GeocodingStats(BaseModel):
    """Geocoding statistics."""
    total_requests: int
    successful: int
    failed: int
    success_rate: float
    cache_hits: int
    tier_distribution: TierDistribution
    avg_response_time_ms: int


class StatsResponse(BaseModel):
    """Service statistics response."""
    uptime_seconds: int
    database: DatabaseStats
    cache: CacheStats
    geocoding: GeocodingStats


class ServiceInfo(BaseModel):
    """API information response."""
    service: str
    version: str
    description: str
    endpoints: Dict[str, str]
    tiers: Dict[str, str]


class AttributionInfo(BaseModel):
    """Required attribution statement for one active data source."""
    source_key: str
    source_name: Optional[str] = None
    required_attribution: Optional[str] = None
    license_type: Optional[str] = None
    license_url: Optional[str] = None


class AttributionsResponse(BaseModel):
    """Response for GET /v1/attributions: required attributions for every
    active (status='confirmed') data source that has one."""
    attributions: List[AttributionInfo]


class ErrorDetail(BaseModel):
    """Machine-readable error detail."""
    code: str  # stable snake_case identifier, e.g. "address_not_found"
    message: str  # human-readable explanation
    details: Optional[Any] = None  # optional extra context (validation errors, limits, ...)


class ErrorResponse(BaseModel):
    """Standard error envelope used by every error response."""
    error: ErrorDetail
