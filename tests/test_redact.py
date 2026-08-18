from comma_osm_speed._redact import redact_route_name


def test_masks_dongle_half():
    assert (
        redact_route_name("a1b2c3d4e5f60718|2026-05-19--14-03-22")
        == "***|2026-05-19--14-03-22"
    )


def test_leaves_names_without_separator_alone():
    assert redact_route_name("2026-05-19--14-03-22") == "2026-05-19--14-03-22"


def test_handles_empty_string():
    assert redact_route_name("") == ""


def test_masks_even_when_route_id_contains_pipes():
    assert redact_route_name("dongle|a|b") == "***|a|b"
