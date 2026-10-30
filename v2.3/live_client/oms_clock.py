"""Bounded independent SNTP clock for OMS execution; never changes OS time."""
from datetime import datetime, timedelta, timezone
import socket
import math
import statistics
import struct
import time

CHINA=timezone(timedelta(hours=8))
NTP_EPOCH=2208988800

def _stamp(data,offset):
    seconds,fraction=struct.unpack_from('!II',data,offset)
    return seconds-NTP_EPOCH+fraction/(2**32)

def query_ntp(host):
    addresses=socket.getaddrinfo(host,123,socket.AF_INET,socket.SOCK_DGRAM)
    if not addresses:raise RuntimeError('NTP resolution empty')
    address=addresses[0][4]
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.connect(address)
        packet=bytearray(48);packet[0]=0x23
        t1=time.time();m1=time.monotonic()
        transmit=t1+NTP_EPOCH
        struct.pack_into('!II',packet,40,int(transmit),int((transmit-int(transmit))*(2**32)))
        client.send(packet)
        reply=client.recv(512)
        m4=time.monotonic();t4=time.time()
    rtt=m4-m1
    if len(reply)<48 or reply[0]&7!=4 or reply[0]>>6==3 or not 1<=reply[1]<=15:
        raise RuntimeError('Unsynchronized or invalid NTP response')
    if reply[24:32]!=packet[40:48]:raise RuntimeError('NTP originate timestamp mismatch')
    if rtt>1 or abs((t4-t1)-rtt)>.1:raise RuntimeError('NTP RTT too long or local clock jumped')
    if struct.unpack_from('!I',reply,8)[0]/65536>1:raise RuntimeError('NTP root dispersion exceeds 1 second')
    t2=_stamp(reply,32);t3=_stamp(reply,40)
    if t3<t2 or t3-t2>rtt+.05:raise RuntimeError('Invalid NTP server timestamp interval')
    offset=((t2-t1)+(t3-t4))/2
    if abs(offset)>300:raise RuntimeError('Clock offset exceeds temporary correction bound')
    return {'host':host,'ip':address[0],'stratum':reply[1],'rtt_seconds':rtt,
            'offset_seconds':offset,'received_local_unix':t4,'received_monotonic':m4,
            'local_time':datetime.fromtimestamp(t4,CHINA).isoformat(),
            'estimated_reference_time':datetime.fromtimestamp(t4+offset,CHINA).isoformat()}

class CalibratedClock:
    def __init__(self,samples,monotonic=time.monotonic):
        self.monotonic=monotonic
        if len(samples)<4 or len({s['host'] for s in samples})<2 or len({s['ip'] for s in samples})<2:
            raise RuntimeError('Two distinct time sources with two samples each required')
        if any(sum(s['host']==h for s in samples)<2 for h in {s['host'] for s in samples}):
            raise RuntimeError('Time source insufficient repeated samples')
        offsets=[s['offset_seconds'] for s in samples]
        if any(not math.isfinite(s[k]) for s in samples for k in
               ('offset_seconds','rtt_seconds','received_local_unix','received_monotonic')):
            raise RuntimeError('Non-finite calibration sample')
        if max(offsets)-min(offsets)>.75:raise RuntimeError('Time sources disagree or offset unstable')
        if any(abs(s['offset_seconds'])>300 or not 0<=s['rtt_seconds']<=1 for s in samples):
            raise RuntimeError('Unsafe calibration sample')
        self.offset=statistics.median(offsets)
        anchor=max(samples,key=lambda s:s['received_monotonic'])
        self.anchor_monotonic=anchor['received_monotonic']
        self.anchor_unix=anchor['received_local_unix']+self.offset
        self.samples=samples
    def now(self):
        elapsed=self.monotonic()-self.anchor_monotonic
        if not 0<=elapsed<=300:raise RuntimeError('Temporary time calibration expired')
        return datetime.fromtimestamp(self.anchor_unix+elapsed,CHINA)
    def evidence(self):
        return {'mode':'OMS_RUNTIME_CALIBRATED','os_clock_changed':False,'offset_seconds':self.offset,
                'calibrated_time':self.now().isoformat(),'uncorrected_local_time':datetime.now(CHINA).isoformat(),
                'sources':self.samples,'rejected_time_samples':getattr(self,'rejected_samples',[]),'valid_seconds':300,
                'limits':{'max_rtt_seconds':1,'max_offset_seconds':300,'max_source_spread_seconds':.75},
                'note':'Public SNTP cross-check, not cryptographic time authentication. Agent windows and journal use corrected time; OS scheduler is unchanged.'}

def calibrate(query=query_ntp):
    samples=[];rejected=[]
    for host in ('time.windows.com','time.cloudflare.com'):
        good=[]
        # Retry only read-only time measurements, never a broker submission.
        # Exactly the same RTT, stability and independent-source limits apply.
        for attempt in range(4):
            try:good.append(query(host))
            except Exception as exc:rejected.append({'host':host,'attempt':attempt+1,'reason':type(exc).__name__+': '+str(exc)})
            if len(good)==2:break
        if len(good)!=2:raise RuntimeError('Independent time source failed bounded retries: '+host)
        samples.extend(good)
    clock=CalibratedClock(samples);clock.rejected_samples=rejected
    return clock


class RuntimeClock:
    """Refresh before expiry. A failed refresh stops work, never falls back to OS time."""
    def __init__(self, factory=calibrate, on_calibration=None):
        self.factory = factory
        self.on_calibration = on_calibration or (lambda evidence: None)
        self.current = None
        self._refresh()

    def _refresh(self):
        fresh = self.factory()
        self.on_calibration(fresh.evidence())
        self.current = fresh

    def now(self):
        if self.current.monotonic() - self.current.anchor_monotonic >= 240:
            self._refresh()
        return self.current.now()
