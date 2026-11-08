from ai_service.services.video.storyboard import scene_candidates


def row(index, phash, quality=0.5, decision='selected'):
    return {'source_index': index, 'decision': decision, 'quality_score': quality, 'phash': phash}


def test_near_duplicate_burst_keeps_only_the_best_shot():
    photos = [row(0, 'ffff000000000000', .4), row(1, 'ffff000000000001', .9), row(2, '0000ffff0000ffff', .5)]
    chosen = scene_candidates({'photos': photos, 'selected_source_indices': [0, 1, 2]}, limit=36, dedupe=True)
    assert chosen == [1, 2]


def test_large_album_sends_at_most_36_photos_spread_over_the_whole_trip():
    photos = [row(i, f'{(i * 0x9E3779B97F4A7C15) % (1 << 64):016x}', quality=(i % 7) / 7) for i in range(108)]
    chosen = scene_candidates({'photos': photos, 'selected_source_indices': list(range(108))}, limit=36, dedupe=True)
    assert len(chosen) <= 36
    assert min(chosen) < 10 and max(chosen) > 97  # beginning and end of the trip are both represented
