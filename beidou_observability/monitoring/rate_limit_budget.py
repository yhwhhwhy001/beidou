"""API Rate-Limit预算 — INV-009执行预留保护。"""
import time
from dataclasses import dataclass, field
from enum import Enum
class BudgetExhaustionPolicy(str,Enum): DEFER="DEFER"; UNKNOWN="UNKNOWN"; BLOCK="BLOCK"
@dataclass(slots=True)
class EndpointBudget:
    endpoint:str; method:str="GET"; weight_limit:int=1200; monitor_budget:int=240; monitor_used:int=0; reserved_execution_budget:int=240; observed_usage:int=0; backoff_until:float=0.0; backoff_multiplier:float=1.0; window_start:float=field(default_factory=time.monotonic); window_duration:float=60.0
    @property
    def monitor_remaining(self): return max(0,self.monitor_budget-self.monitor_used)
    @property
    def execution_reserve_remaining(self): return max(0,self.reserved_execution_budget-max(0,self.observed_usage-self.monitor_used))
    @property
    def is_exhausted(self): return self.monitor_remaining<=0
    @property
    def is_in_backoff(self): return time.monotonic()<self.backoff_until
    def record_usage(self,weight,headers=None):
        now=time.monotonic()
        if now-self.window_start>self.window_duration: self.window_start=now; self.observed_usage=0; self.monitor_used=0
        self.observed_usage+=weight; self.monitor_used+=weight
        if headers and "X-MBX-USED-WEIGHT-1M" in headers:
            try: self.observed_usage=int(headers["X-MBX-USED-WEIGHT-1M"])
            except: pass
    def handle_rate_limit(self,retry_after=1.0):
        self.backoff_multiplier=min(self.backoff_multiplier*2.0,60.0); self.backoff_until=time.monotonic()+(retry_after*self.backoff_multiplier)
    def try_consume(self,weight,is_critical):
        if self.is_in_backoff: return False,BudgetExhaustionPolicy.UNKNOWN if is_critical else BudgetExhaustionPolicy.DEFER
        if self.monitor_remaining<weight: return False,BudgetExhaustionPolicy.UNKNOWN if is_critical else BudgetExhaustionPolicy.DEFER
        if self.execution_reserve_remaining<=0: return False,BudgetExhaustionPolicy.BLOCK
        return True,BudgetExhaustionPolicy.DEFER
@dataclass(slots=True)
class RateLimitBudget:
    budgets:dict=field(default_factory=dict); execution_reserve_pct:float=0.20
    def register(self,endpoint,*,method="GET",weight_limit=1200,reserved=None,monitor_budget=None):
        key=f"{method}:{endpoint}"; r=reserved or int(weight_limit*self.execution_reserve_pct); m=monitor_budget or int((weight_limit-r)*0.5)
        b=EndpointBudget(endpoint=endpoint,method=method,weight_limit=weight_limit,reserved_execution_budget=r,monitor_budget=m); self.budgets[key]=b; return b
    def get_budget(self,endpoint,method="GET"): return self.budgets.get(f"{method}:{endpoint}")
    def summary(self): return {k:{"endpoint":b.endpoint,"monitor_remaining":b.monitor_remaining,"exec_reserve":b.execution_reserve_remaining,"exhausted":b.is_exhausted} for k,b in self.budgets.items()}
    def is_execution_reserve_intact(self): return all(b.execution_reserve_remaining>0 for b in self.budgets.values() if b.reserved_execution_budget>0)
