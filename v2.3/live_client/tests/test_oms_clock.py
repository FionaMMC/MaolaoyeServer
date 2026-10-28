import unittest
from live_client import oms_clock as c

class ClockTests(unittest.TestCase):
    def samples(self):
        return [{'host':h,'ip':ip,'rtt_seconds':.05,'offset_seconds':65.,'received_local_unix':1791425412.,'received_monotonic':100.}
                for h,ip in [('a','1.1.1.1'),('b','2.2.2.2')] for _ in range(2)]
    def test_offset_and_monotonic_progress(self):
        clock=c.CalibratedClock(self.samples(),monotonic=lambda:103.)
        self.assertEqual(clock.now().timestamp(),1791425480.)
    def test_local_clock_jump_does_not_change_anchor(self):
        clock=c.CalibratedClock(self.samples(),monotonic=lambda:101.)
        self.assertEqual(clock.now().timestamp(),1791425478.)
    def test_source_disagreement_blocks(self):
        samples=self.samples();samples[-1]['offset_seconds']=67.
        with self.assertRaises(RuntimeError):c.CalibratedClock(samples)
    def test_one_source_blocks(self):
        with self.assertRaises(RuntimeError):c.CalibratedClock(self.samples()[:2])
    def test_same_ip_not_independent(self):
        samples=self.samples()
        for s in samples:s['ip']='1.1.1.1'
        with self.assertRaises(RuntimeError):c.CalibratedClock(samples)
    def test_expiration_blocks(self):
        clock=c.CalibratedClock(self.samples(),monotonic=lambda:401.)
        with self.assertRaises(RuntimeError):clock.now()
    def test_excessive_delay_blocks(self):
        samples=self.samples();samples[-1]['rtt_seconds']=2.
        with self.assertRaises(RuntimeError):c.CalibratedClock(samples)
    def test_query_failure_not_silently_ignored(self):
        def bad(_):raise TimeoutError('No independent time')
        with self.assertRaises(RuntimeError):c.calibrate(bad)

    def test_nonfinite_sample_blocks(self):
        samples=self.samples();samples[0]['offset_seconds']=float('nan')
        with self.assertRaises(RuntimeError):c.CalibratedClock(samples)

    def test_runtime_refreshes_and_never_falls_back_after_failure(self):
        mono=[100.]
        calls=[]
        def factory():
            calls.append(True)
            if len(calls)>1:raise TimeoutError('refresh failed')
            return c.CalibratedClock(self.samples(),monotonic=lambda:mono[0])
        receipts=[]
        runtime=c.RuntimeClock(factory=factory,on_calibration=receipts.append)
        self.assertEqual(len(receipts),1)
        mono[0]=340.
        with self.assertRaises(TimeoutError):runtime.now()
        self.assertEqual(len(calls),2)

if __name__=='__main__':unittest.main()
