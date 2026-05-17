from ecourts_client.transport import RawTransport


def test_raw_transport_constructs():
    t = RawTransport()
    assert callable(t.fetch_case)
    assert callable(t.fetch_pdf)
