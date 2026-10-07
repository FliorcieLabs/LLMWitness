"""Side-effect-free local demonstration of UNKNOWN reconciliation."""

import asyncio
import tempfile
from pathlib import Path

from llmwitness.authority import AuthorityResult
from llmwitness.contracts import AgentContract
from llmwitness.effects import EffectContext, SafeEffectRunner
from llmwitness.envelope import Actor, AuthorityDecision, RiskTier, new_trace_id
from llmwitness.journal import SQLiteJournalStore
from llmwitness.reference_effects import InMemoryRefundEffect
from llmwitness.reliability import ChaosEngine, FaultInjector, FaultPoint
from llmwitness.utils import generate_uuidv7


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with SQLiteJournalStore(Path(directory) / "journal.db") as journal:
            adapter = InMemoryRefundEffect(
                chaos=ChaosEngine(FaultInjector({FaultPoint.RESPONSE_LOSS}))
            )
            context = EffectContext(
                run_id=generate_uuidv7(),
                trace_id=new_trace_id(),
                actor=Actor(agent_id="demo-agent", principal_id="local-user"),
                authority=AuthorityResult(
                    "demo-policy", AuthorityDecision.ALLOW, RiskTier.R2, "demo allow"
                ),
                contract=AgentContract("demo-contract"),
            )
            runner = SafeEffectRunner(journal)
            first = await runner.run(
                adapter, {"payment_id": "pay-demo", "amount": 3499}, context
            )
            adapter.chaos = ChaosEngine()
            reconciled = await runner.run(
                adapter, {"payment_id": "pay-demo", "amount": 3499}, context
            )
            print(first.state.value, reconciled.state.value, adapter.execute_count)


if __name__ == "__main__":
    asyncio.run(main())
