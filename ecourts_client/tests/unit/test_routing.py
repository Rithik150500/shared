from ecourts_client.routing import classify_cnr, validate_cnr_shape
from ecourts_client.errors import CNRMalformed
import pytest


def test_district_cnr():
    assert classify_cnr("MHCC010054732024") == "district"


def test_highcourt_cnr():
    assert classify_cnr("MHHC010001232024") == "highcourt"


def test_malformed_cnr_raises():
    with pytest.raises(CNRMalformed):
        validate_cnr_shape("not-a-cnr")


def test_unknown_state_code_raises():
    with pytest.raises(CNRMalformed):
        validate_cnr_shape("ZZCC010054732024")
