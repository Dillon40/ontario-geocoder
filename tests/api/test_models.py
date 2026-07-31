"""
Tests for api/models.py — Pydantic model validation.
"""
import pytest
from pydantic import ValidationError
from api.models import (
    GeocodeRequest,
    BatchGeocodeRequest,
    HealthResponse,
    IDXAddress,
    ServiceInfo,
)


class TestGeocodeRequest:
    """Tests for GeocodeRequest model."""

    def test_idx_format(self):
        req = GeocodeRequest(
            Address=IDXAddress(
                StreetNumber='100',
                StreetName='Queen',
                StreetSuffix='St',
                City='Toronto'
            )
        )
        assert req.Address.StreetNumber == '100'

    def test_string_address(self):
        req = GeocodeRequest(address='100 Queen St, Toronto')
        assert req.address == '100 Queen St, Toronto'

    def test_flat_dict_snake(self):
        req = GeocodeRequest(street_number='100', street_name='Queen')
        assert req.street_number == '100'
        assert req.street_name == 'Queen'

    def test_flat_dict_camel(self):
        req = GeocodeRequest(streetNumber='100', streetName='Queen')
        assert req.streetNumber == '100'
        assert req.streetName == 'Queen'

    def test_pin(self):
        req = GeocodeRequest(pin='12345678')
        assert req.pin == '12345678'

    def test_pin_uppercase(self):
        req = GeocodeRequest(PIN='12345678')
        assert req.PIN == '12345678'

    def test_mls_number(self):
        req = GeocodeRequest(mls_number='W12345')
        assert req.mls_number == 'W12345'

    def test_extra_fields_rejected(self):
        """Unknown/typo'd fields should be rejected (Config.extra = 'forbid')."""
        with pytest.raises(ValidationError):
            GeocodeRequest(custom_field='value')

    def test_all_optional(self):
        """All fields are optional — empty request is valid."""
        req = GeocodeRequest()
        assert req.Address is None
        assert req.address is None

    def test_postal_code_variants(self):
        req1 = GeocodeRequest(postal_code='M5V 1A1')
        req2 = GeocodeRequest(postalCode='M5V 1A1')
        assert req1.postal_code == 'M5V 1A1'
        assert req2.postalCode == 'M5V 1A1'


class TestBatchGeocodeRequest:
    """Tests for BatchGeocodeRequest model."""

    def test_valid_list(self):
        req = BatchGeocodeRequest(addresses=[{'street_name': 'Queen'}])
        assert len(req.addresses) == 1

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            BatchGeocodeRequest(addresses=[])

    def test_max_1000(self):
        """Should accept up to 1000 addresses."""
        req = BatchGeocodeRequest(addresses=[{'a': i} for i in range(1000)])
        assert len(req.addresses) == 1000

    def test_over_max_rejected(self):
        with pytest.raises(ValidationError):
            BatchGeocodeRequest(addresses=[{'a': i} for i in range(1001)])

    def test_missing_addresses_rejected(self):
        with pytest.raises(ValidationError):
            BatchGeocodeRequest()


class TestHealthResponse:
    """Tests for HealthResponse model."""

    def test_required_fields(self):
        resp = HealthResponse(
            status='healthy',
            database='connected',
            redis='connected',
            timestamp='2024-01-01T00:00:00'
        )
        assert resp.status == 'healthy'
        assert resp.database == 'connected'
        assert resp.redis == 'connected'

    def test_serialization(self):
        resp = HealthResponse(
            status='healthy',
            database='connected',
            redis='connected',
            timestamp='2024-01-01T00:00:00'
        )
        data = resp.model_dump()
        assert data['status'] == 'healthy'
        assert 'timestamp' in data

    def test_missing_fields_rejected(self):
        with pytest.raises(ValidationError):
            HealthResponse(status='healthy')


class TestServiceInfo:
    """Tests for ServiceInfo model."""

    def test_required_fields(self):
        info = ServiceInfo(
            service='Geocoder',
            version='1.0.0',
            description='Test',
            endpoints={'GET /': 'root'},
            tiers={'tier_1': 'PIN lookup'}
        )
        assert info.service == 'Geocoder'
        assert info.version == '1.0.0'
