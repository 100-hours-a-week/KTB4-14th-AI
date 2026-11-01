"""사용자가 정한 원화 예산으로 편당 총비용과 GPU 시간의 계획 상한을 계산한다."""


def production_budget(*, budget_krw=320_000, target_videos=500,
                      planning_krw_per_usd=1500, hourly_usd=1.11, reserve_fraction=0.20):
    if min(budget_krw, target_videos, planning_krw_per_usd, hourly_usd) <= 0 or not 0 <= reserve_fraction < 1:
        raise ValueError('invalid video budget')
    available = budget_krw * (1 - reserve_fraction)
    per_video = available / target_videos
    rate_krw = hourly_usd * planning_krw_per_usd
    return {
        'budget_krw': budget_krw, 'target_videos': target_videos,
        'target_date_kst': '2026-11-17',
        'total_cost_ceiling_per_video_krw': budget_krw / target_videos,
        'reserve_krw': budget_krw * reserve_fraction,
        'reserve_fraction': reserve_fraction, 'available_for_gpu_krw': available,
        'planning_krw_per_usd': planning_krw_per_usd,
        'exchange_rate_note': 'Conservative planning assumption, not a live exchange-rate quote.',
        'hourly_usd_reference': hourly_usd,
        'gpu_cost_ceiling_per_video_krw': per_video,
        'gpu_minutes_ceiling_per_video': per_video / rate_krw * 60,
        'gpu_hours_ceiling_total': available / rate_krw,
        'cost_scope': 'GPU allowance includes setup, idle time and failed attempts. Reserve covers storage, retries, FX/payment differences and remaining overhead; actual invoices are required.',
        'scenario_estimates': [
            {'gpu_minutes_per_video': minutes, 'gpu_krw_per_video': rate_krw * minutes / 60,
             'gpu_krw_total': rate_krw * minutes / 60 * target_videos,
             'within_gpu_allowance': rate_krw * minutes / 60 * target_videos <= available}
            for minutes in (5, 10, 15, 20)
        ],
        'production_feasibility': 'not_validated',
    }
