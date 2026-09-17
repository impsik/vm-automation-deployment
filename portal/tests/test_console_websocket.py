from app import PortalHandler


def test_portal_uses_http_11_for_websocket_upgrades():
    assert PortalHandler.protocol_version == "HTTP/1.1"


def test_portal_does_not_buffer_websocket_frames_in_http_reader():
    assert PortalHandler.rbufsize == 0
