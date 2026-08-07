"""Order Pipeline Instrumentation (MON05A)。"""
import time
from dataclasses import dataclass, field
from beidou_observability.monitoring.contracts import TraceStage
@dataclass(slots=True)
class TraceEvent:
    trace_id:str; correlation_id:str; strategy_id:str; intent_id:str; client_order_id:str; stage:TraceStage; source_timestamp:float; observed_at:float=field(default_factory=time.monotonic); symbol:str=""; side:str=""; quantity:str=""; price:str=""
@dataclass(slots=True)
class InstrumentationPolicy:
    fail_open_on_exit_paths:bool=True; fail_closed_on_entry_paths:bool=True; append_only:bool=True; overhead_budget_us:int=500
@dataclass(slots=True)
class InstrumentationRecorder:
    events:list=field(default_factory=list); policy:InstrumentationPolicy=field(default_factory=InstrumentationPolicy)
    def record(self,event):
        start=time.perf_counter(); self.events.append(event)
        return (time.perf_counter()-start)*1e6<=self.policy.overhead_budget_us
    def get_by_correlation(self,cid): return [e for e in self.events if e.correlation_id==cid]
    def stage_completed(self,cid,stage): return any(e.correlation_id==cid and e.stage==stage for e in self.events)
