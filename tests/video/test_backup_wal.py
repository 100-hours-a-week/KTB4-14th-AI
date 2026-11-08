from scripts.video.backup import wal_file


def test_wal_file_name_for_default_and_small_segments():
    assert wal_file(1, '0/2000028', 16 * 1024 * 1024) == '000000010000000000000002'
    assert wal_file(1, '0/2000028', 1024 * 1024) == '000000010000000000000020'
    assert wal_file(2, '1/A0000000', 1024 * 1024) == '000000020000000100000A00'
