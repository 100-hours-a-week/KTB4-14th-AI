import unittest

from ai_service.services.video.budget import production_budget


class BudgetTests(unittest.TestCase):
    def test_won_budget_accounts_for_reserve_and_does_not_claim_feasibility(self):
        budget = production_budget()
        self.assertEqual(budget['total_cost_ceiling_per_video_krw'], 640)
        self.assertEqual(budget['reserve_krw'], 64000)
        self.assertEqual(budget['gpu_cost_ceiling_per_video_krw'], 512)
        self.assertAlmostEqual(budget['gpu_minutes_ceiling_per_video'], 18.45045, places=4)
        self.assertEqual(budget['production_feasibility'], 'not_validated')
        scenarios = budget['scenario_estimates']
        self.assertTrue(next(r for r in scenarios if r['gpu_minutes_per_video'] == 15)['within_gpu_allowance'])
        self.assertFalse(next(r for r in scenarios if r['gpu_minutes_per_video'] == 20)['within_gpu_allowance'])
