from unittest import TestCase
from bot.backtest import execute, volume_profile, unmitigated_order_blocks


class BacktestTests(TestCase):
    def trade(self):
        return dict(direction='LONG', entry=100, stop=90, tp1=120, tp2=140)

    def test_profile_value_area_contains_poc(self):
        p=volume_profile([{'hist': {'100': 10, '101': 8, '102': 1}}])
        self.assertEqual(p['poc'],100); self.assertLessEqual(p['val'],p['poc']); self.assertGreaterEqual(p['vah'],p['poc'])

    def test_same_minute_stop_is_adverse(self):
        r=execute(self.trade(),[{'t':1,'l':89,'h':141,'c':130}],0)
        self.assertEqual(r['exit_reason'],'STOP'); self.assertFalse(r['tp1_hit']); self.assertLess(r['pnl_r'],-1)

    def test_partial_tp_then_stop(self):
        r=execute(self.trade(),[{'t':1,'l':99,'h':121,'c':120},{'t':2,'l':89,'h':119,'c':90}],0)
        self.assertTrue(r['tp1_hit']); self.assertEqual(r['exit_reason'],'STOP'); self.assertGreater(r['pnl_r'],0)

    def test_order_block_before_imbalance_is_found_until_revisited(self):
        bars=[{'open':100,'close':99,'high':101,'low':98},{'open':99,'close':100,'high':101,'low':98},
              {'open':105,'close':106,'high':107,'low':105},{'open':106,'close':106,'high':107,'low':105}]
        self.assertIn((99,100),unmitigated_order_blocks(bars,3,'LONG'))
        bars.append({'open':101,'close':100,'high':102,'low':99})
        self.assertIn((99,100),unmitigated_order_blocks(bars,4,'LONG'))
        bars.append({'open':100,'close':100,'high':101,'low':99})
        self.assertNotIn((99,100),unmitigated_order_blocks(bars,5,'LONG'))
