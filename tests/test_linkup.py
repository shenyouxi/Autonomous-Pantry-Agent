from pantry.tools.linkup import FakeLinkupClient


def test_fake_linkup_is_deterministic():
    client = FakeLinkupClient()

    first = client.search("double cream substitutes")
    second = client.search("double cream substitutes")

    assert first == second
    assert isinstance(first["answer"], str)
    assert first["sources"]

    for source in first["sources"]:
        assert set(source) == {"title", "url", "content"}