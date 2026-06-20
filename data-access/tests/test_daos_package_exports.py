def test_new_daos_are_exported():
    from data_access import daos

    assert "login_request_dao" in daos.__all__
    assert "email_otp_dao" in daos.__all__
    assert hasattr(daos, "login_request_dao")
    assert hasattr(daos, "email_otp_dao")
    assert callable(daos.login_request_dao.confirm)
    assert callable(daos.email_otp_dao.mark_used)
