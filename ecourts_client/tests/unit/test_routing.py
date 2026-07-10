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


# eCourts uses TWO High Court CNR conventions. The [STATE][HC] form (e.g.
# 'MHHC...', 'KAHC...') is handled above. The [HC][bench] form puts the literal
# 'HC' in the state slot (chars 0:2) and the bench code in chars 2:4 -- e.g.
# Bombay HC = 'HCBM...', Madras HC = 'HCMA...'. These are REAL CNRs the eCourts
# HC portal returns and fetch_case resolves; only validate_cnr_shape rejected
# them ("unknown state code 'HC'"). No geographic state is ever 'HC', so an
# 'HC' state slot is unambiguously a High Court.
def test_hc_prefixed_bombay_cnr_validates():
    validate_cnr_shape("HCBM010091352019")  # must not raise


def test_hc_prefixed_bombay_cnr_classifies_highcourt():
    assert classify_cnr("HCBM010091352019") == "highcourt"


def test_hc_prefixed_madras_cnr_classifies_highcourt():
    assert classify_cnr("HCMA010745952023") == "highcourt"


def test_numeric_establishment_cnr_accepted():
    # Real Madhya Pradesh district CNR: establishment code is numeric ('20'),
    # so chars 2:4 are digits. The old [A-Z]{2}[A-Z]{2}... regex wrongly
    # rejected these; a valid state ('MP') + 16-char shape must classify as
    # district. Regression for "CNR validation failed for Madhya Pradesh".
    validate_cnr_shape("MP20060042872025")  # should not raise
    assert classify_cnr("MP20060042872025") == "district"


def test_numeric_establishment_unknown_state_still_raises():
    # Shape now allows numeric establishment, but the state whitelist still
    # guards district CNRs: 'ZZ' is not a real state code.
    with pytest.raises(CNRMalformed):
        validate_cnr_shape("ZZ20060042872025")
